from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
import yaml

from scripts.release.check_phase9_readiness import (
    main as readiness_main,
)
from scripts.release.check_phase9_readiness import (
    validate_compatibility,
    validate_readiness,
)
from scripts.reliability.phase9_http_probe import (
    HttpSample,
    percentile,
    summarize,
    validate_target,
)

ROOT = Path(__file__).resolve().parents[2]


def test_phase9_records_are_structurally_valid_but_fail_closed_for_1_0(
    capsys: pytest.CaptureFixture[str],
) -> None:
    readiness = validate_readiness(ROOT, ROOT / "release/phase9-readiness.yaml")

    assert readiness.errors == ()
    assert len(readiness.incomplete) == 60
    assert not readiness.releasable
    assert validate_compatibility(ROOT, ROOT / "compatibility/hardware.yaml") == ()
    assert readiness_main(["--root", str(ROOT), "--allow-incomplete"]) == 0
    assert "release gates remain open" in capsys.readouterr().out
    assert readiness_main(["--root", str(ROOT)]) == 1
    assert "not ready for 1.0" in capsys.readouterr().err


def test_readiness_validator_requires_complete_unique_criterion_inventory(tmp_path: Path) -> None:
    path = tmp_path / "readiness.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "schema_version": 1,
                "phase": 9,
                "target_release": "1.0.0",
                "overall_status": "READY",
                "criteria": [
                    {"id": 1, "requirement": "first", "gate": "automated", "state": "passed"},
                    {"id": 1, "requirement": "again", "gate": "review", "state": "passed"},
                ],
            }
        ),
        encoding="utf-8",
    )

    result = validate_readiness(tmp_path, path)

    assert any("duplicated" in error for error in result.errors)
    assert any("missing IDs" in error for error in result.errors)


def test_compatibility_validator_cannot_verify_hardware_without_evidence(
    tmp_path: Path,
) -> None:
    path = tmp_path / "compatibility.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "schema_version": 1,
                "status_definitions": {
                    status: status.casefold()
                    for status in (
                        "VERIFIED",
                        "SUPPORTED",
                        "EXPERIMENTAL",
                        "COMMUNITY",
                        "DEPRECATED",
                    )
                },
                "plugins": {
                    "unsafe-claim": {
                        "status": "VERIFIED",
                        "implementation_state": "implemented",
                        "latest_physical_evidence": None,
                    }
                },
                "accessories": {},
            }
        ),
        encoding="utf-8",
    )

    errors = validate_compatibility(tmp_path, path)

    assert any("VERIFIED without latest_physical_evidence" in error for error in errors)


def test_http_probe_validates_targets_and_calculates_nearest_rank_percentiles() -> None:
    assert validate_target("https://lab.example.com/", "/api/v1/health") == (
        "https://lab.example.com/api/v1/health"
    )
    assert percentile([1.0, 4.0, 2.0, 3.0], 0.95) == 4.0
    with pytest.raises(ValueError, match="credentials"):
        validate_target("https://token@lab.example.com", "/api/v1/health")
    with pytest.raises(ValueError, match="query"):
        validate_target("https://lab.example.com?token=secret", "/api/v1/health")


def test_http_probe_summary_is_scoped_and_fails_on_any_request_error() -> None:
    successful = [
        HttpSample(latency_ms=10.0, status_code=200, error=None),
        HttpSample(latency_ms=20.0, status_code=200, error=None),
    ]
    passed = summarize(
        successful,
        profile="smoke",
        public_target="http://127.0.0.1:8443/api/v1/health",
        started_at=datetime(2026, 8, 26, tzinfo=UTC),
        duration_seconds=1,
        concurrency=2,
        maximum_p95_ms=500,
    )
    assert passed["status"] == "PASS"
    assert "does not by itself close" in str(passed["scope"])

    failed = summarize(
        [*successful, HttpSample(latency_ms=5.0, status_code=503, error="HTTP 503")],
        profile="reference",
        public_target="http://127.0.0.1:8443/api/v1/health",
        started_at=datetime(2026, 8, 26, tzinfo=UTC),
        duration_seconds=1,
        concurrency=2,
        maximum_p95_ms=500,
    )
    assert failed["status"] == "FAIL"
    assert failed["error_counts"] == {"HTTP 503": 1}


def test_reliability_manifest_and_evidence_template_are_explicitly_not_run() -> None:
    manifest = yaml.safe_load(
        (ROOT / "tests/reliability/phase9-reference.yaml").read_text(encoding="utf-8")
    )
    template = yaml.safe_load(
        (ROOT / "tests/reliability/evidence-template.yaml").read_text(encoding="utf-8")
    )

    assert manifest["status"] == "EVIDENCE_NOT_RUN"
    assert manifest["soak"]["minimum_duration_hours"] == 24
    assert manifest["acceptance"]["release_gate_requires"]
    assert template["status"] == "NOT_RUN"
    assert template["review"]["assertions_reviewed"] is False
    assert template["physical"]["second_mcu"]["status"] == "NOT_RUN"
