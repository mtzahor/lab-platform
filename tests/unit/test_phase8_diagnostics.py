from __future__ import annotations

import shutil
import tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest
from lab_platform.control_plane import diagnostics
from lab_platform.control_plane.config import ControlPlaneConfig
from lab_platform.core.artifact_storage import S3CompatibleArtifactStorage
from lab_platform.persistence import SCHEMA_VERSION
from lab_platform.persistence.database_management import SchemaStatus


def _development_config(tmp_path: Path, **extra: object) -> ControlPlaneConfig:
    payload: dict[str, object] = {
        "control_plane": {
            "host": "127.0.0.1",
            "public_url": "http://127.0.0.1:8443",
        },
        "database": {"url": f"sqlite:///{tmp_path / 'control-plane.db'}"},
        "artifacts": {"directory": tmp_path / "artifacts"},
        "development": {"allow_insecure_agent_transport": True},
    }
    payload.update(extra)
    return ControlPlaneConfig.model_validate(payload)


def _checks(report: diagnostics.DiagnosticReport) -> dict[str, diagnostics.DiagnosticCheck]:
    return {check.name: check for check in report.checks}


def _direct_tls_config(tmp_path: Path, certificate: Path, private_key: Path) -> ControlPlaneConfig:
    return ControlPlaneConfig.model_validate(
        {
            "control_plane": {
                "host": "127.0.0.1",
                "public_url": "https://127.0.0.1:8443",
                "tls_certificate_path": certificate,
                "tls_private_key_path": private_key,
            },
            "database": {"url": f"sqlite:///{tmp_path / 'control-plane.db'}"},
            "artifacts": {"directory": tmp_path / "artifacts"},
        }
    )


def test_doctor_reports_missing_unwritable_and_low_space_artifact_storage(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _development_config(tmp_path)
    missing = _checks(diagnostics.collect_diagnostics(config, check_database=False))
    assert missing["artifact_directory"].status == "error"
    assert "missing" in missing["artifact_directory"].message

    config.artifacts.directory.mkdir()

    def refuse_write(*_args: object, **_kwargs: object) -> object:
        raise OSError("read-only filesystem")

    monkeypatch.setattr(tempfile, "NamedTemporaryFile", refuse_write)
    unwritable = _checks(diagnostics.collect_diagnostics(config, check_database=False))
    assert unwritable["artifact_directory"].status == "error"
    assert "not writable" in unwritable["artifact_directory"].message

    monkeypatch.undo()
    monkeypatch.setattr(
        shutil,
        "disk_usage",
        lambda _path: SimpleNamespace(total=1000, used=950, free=50),
    )
    low_space = _checks(diagnostics.collect_diagnostics(config, check_database=False))
    assert low_space["artifact_directory"].status == "ok"
    assert low_space["disk_space"].status == "warning"


def test_doctor_reports_database_outage_and_incompatible_schema(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _development_config(tmp_path)

    def unavailable(_url: str) -> SchemaStatus:
        raise OSError("database unavailable")

    monkeypatch.setattr(diagnostics, "inspect_database_schema", unavailable)
    outage = _checks(diagnostics.collect_diagnostics(config, check_filesystem=False))
    assert outage["database_connection"].status == "error"
    assert "unavailable" in outage["database_connection"].message

    incompatible = SchemaStatus(
        backend="sqlite",
        current_version=SCHEMA_VERSION - 1,
        target_version=SCHEMA_VERSION,
        minimum_supported_version=SCHEMA_VERSION - 1,
        applied_versions=tuple(range(2, SCHEMA_VERSION)),
        state="upgrade_required",
    )
    monkeypatch.setattr(diagnostics, "inspect_database_schema", lambda _url: incompatible)
    schema = _checks(diagnostics.collect_diagnostics(config, check_filesystem=False))
    assert schema["database_connection"].status == "ok"
    assert schema["database_schema"].status == "error"
    assert "must be migrated" in schema["database_schema"].message


def test_production_check_fails_closed_for_secrets_limits_and_oidc(
    tmp_path: Path,
) -> None:
    config = ControlPlaneConfig.model_validate(
        {
            "profile": "production",
            "control_plane": {
                "host": "0.0.0.0",
                "public_url": "https://lab.example.com",
            },
            "database": {
                "url": "postgresql://lab@database/lab_platform",
                "pool_size": 1,
            },
            "artifacts": {"directory": tmp_path / "artifacts"},
            "identity": {
                "oidc": {
                    "enabled": True,
                    "issuer_url": "https://identity.example.com",
                    "client_id": "lab-platform",
                    "client_secret_env": "LAB_OIDC_SECRET",
                }
            },
            "proxy": {"enabled": True, "trusted_networks": ["127.0.0.1/32"]},
        }
    )

    report = diagnostics.collect_diagnostics(
        config,
        environ={},
        check_database=False,
        check_filesystem=False,
        require_production=True,
    )
    checks = _checks(report)

    assert report.ok is False
    assert checks["secret_key"].status == "error"
    assert checks["oidc_secret"].status == "error"
    assert checks["database_pool"].status == "error"
    assert checks["resource_limits"].status == "error"
    assert checks["api_rate_limits"].status == "error"


def test_doctor_reports_s3_object_store_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _development_config(
        tmp_path,
        artifacts={
            "storage_backend": "s3",
            "directory": tmp_path / "unused",
            "s3": {"bucket": "lab-artifacts", "prefix": "phase8"},
        },
    )

    async def unavailable(
        _storage: S3CompatibleArtifactStorage,
        _key: str,
    ) -> bool:
        raise OSError("object store offline")

    monkeypatch.setattr(S3CompatibleArtifactStorage, "exists", unavailable)
    report = diagnostics.collect_diagnostics(config, check_database=False)
    storage = _checks(report)["artifact_storage"]
    assert storage.status == "error"
    assert "object store offline" in storage.message


def test_diagnostic_report_serializes_status_and_warning_count() -> None:
    report = diagnostics.DiagnosticReport(
        (
            diagnostics.DiagnosticCheck("configuration", "ok", "loaded"),
            diagnostics.DiagnosticCheck("capacity", "warning", "low"),
            diagnostics.DiagnosticCheck("database", "error", "offline"),
        )
    )

    assert report.ok is False
    assert report.warnings == 1
    assert report.as_dict() == {
        "ok": False,
        "warnings": 1,
        "checks": [
            {"name": "configuration", "status": "ok", "message": "loaded"},
            {"name": "capacity", "status": "warning", "message": "low"},
            {"name": "database", "status": "error", "message": "offline"},
        ],
    }


def test_production_check_rejects_non_production_profile(tmp_path: Path) -> None:
    report = diagnostics.collect_diagnostics(
        _development_config(tmp_path),
        check_database=False,
        check_filesystem=False,
        require_production=True,
    )

    assert _checks(report)["profile"].status == "error"


def test_doctor_checks_direct_tls_paths_and_readability(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    certificate = tmp_path / "server.crt"
    private_key = tmp_path / "server.key"
    config = _direct_tls_config(tmp_path, certificate, private_key)

    offline = _checks(
        diagnostics.collect_diagnostics(
            config,
            check_database=False,
            check_filesystem=False,
        )
    )
    assert offline["tls"].status == "ok"

    missing = _checks(diagnostics.collect_diagnostics(config, check_database=False))
    assert missing["tls"].status == "error"
    assert "missing TLS file" in missing["tls"].message

    certificate.write_text("certificate\n", encoding="utf-8")
    private_key.write_text("private key\n", encoding="utf-8")
    readable = _checks(diagnostics.collect_diagnostics(config, check_database=False))
    assert readable["tls"].status == "ok"

    monkeypatch.setattr(
        "lab_platform.control_plane.diagnostics.os.access",
        lambda _path, _mode: False,
    )
    unreadable = _checks(diagnostics.collect_diagnostics(config, check_database=False))
    assert unreadable["tls"].status == "error"
    assert "unreadable TLS file" in unreadable["tls"].message


def test_doctor_checks_configured_application_and_oidc_secrets(tmp_path: Path) -> None:
    base = {
        "control_plane": {
            "host": "127.0.0.1",
            "public_url": "http://127.0.0.1:8443",
        },
        "database": {"url": f"sqlite:///{tmp_path / 'control-plane.db'}"},
        "artifacts": {"directory": tmp_path / "artifacts"},
        "development": {"allow_insecure_agent_transport": True},
        "identity": {
            "oidc": {
                "enabled": True,
                "issuer_url": "https://identity.example.com",
                "client_id": "lab-platform",
                "client_secret_env": "LAB_OIDC_SECRET",
            }
        },
    }
    weak = ControlPlaneConfig.model_validate(
        {**base, "security": {"secret_key": "same-character-secret-same-character"}}
    )
    weak_report = _checks(
        diagnostics.collect_diagnostics(
            weak,
            environ={
                "LAB_OIDC_SECRET": "one",
                "LAB_OIDC_SECRET_FILE": str(tmp_path / "secret"),
            },
            check_database=False,
            check_filesystem=False,
        )
    )
    assert weak_report["secret_key"].status == "warning"
    assert weak_report["oidc_secret"].status == "error"
    assert "Set only one" in weak_report["oidc_secret"].message

    strong = ControlPlaneConfig.model_validate(
        {**base, "security": {"secret_key": "A9!b2@C3#d4$E5%f6^G7&h8*I9(j0)K1"}}
    )
    strong_report = _checks(
        diagnostics.collect_diagnostics(
            strong,
            environ={"LAB_OIDC_SECRET": "available"},
            check_database=False,
            check_filesystem=False,
        )
    )
    assert strong_report["secret_key"].status == "ok"
    assert strong_report["oidc_secret"].status == "ok"


def test_doctor_reports_offline_and_reachable_s3_storage(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _development_config(
        tmp_path,
        artifacts={
            "storage_backend": "s3",
            "directory": tmp_path / "unused",
            "s3": {"bucket": "lab-artifacts", "prefix": "phase8"},
        },
    )
    offline = _checks(
        diagnostics.collect_diagnostics(
            config,
            check_database=False,
            check_filesystem=False,
        )
    )
    assert offline["artifact_storage"].status == "ok"
    assert "configured" in offline["artifact_storage"].message

    async def reachable(
        _storage: S3CompatibleArtifactStorage,
        _key: str,
    ) -> bool:
        return False

    monkeypatch.setattr(S3CompatibleArtifactStorage, "exists", reachable)
    online = _checks(diagnostics.collect_diagnostics(config, check_database=False))
    assert online["artifact_storage"].status == "ok"
    assert "reachable" in online["artifact_storage"].message


def test_doctor_reports_non_directory_and_disabled_local_storage(tmp_path: Path) -> None:
    artifact_path = tmp_path / "artifact-file"
    artifact_path.write_text("not a directory\n", encoding="utf-8")
    config = _development_config(tmp_path, artifacts={"directory": artifact_path})

    disabled = _checks(
        diagnostics.collect_diagnostics(
            config,
            check_database=False,
            check_filesystem=False,
        )
    )
    assert disabled["artifact_directory"].status == "ok"

    enabled = _checks(diagnostics.collect_diagnostics(config, check_database=False))
    assert enabled["artifact_directory"].status == "error"
    assert "not a directory" in enabled["artifact_directory"].message


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("short", False),
        ("a" * 32, False),
        ("change-me-change-me-change-me-change-me", False),
        ("A9!b2@C3#d4$E5%f6^G7&h8*I9(j0)K1", True),
    ],
)
def test_secret_strength_policy(value: str, expected: bool) -> None:
    assert diagnostics._strong_secret(value) is expected
