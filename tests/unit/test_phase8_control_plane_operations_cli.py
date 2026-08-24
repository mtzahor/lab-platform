from __future__ import annotations

import json
from pathlib import Path

import pytest
from lab_platform.control_plane import cli as control_plane_cli
from lab_platform.control_plane.config import ControlPlaneConfig
from lab_platform.control_plane.diagnostics import DiagnosticCheck, DiagnosticReport
from lab_platform.persistence import SCHEMA_VERSION


def _config(tmp_path: Path, *, database_name: str = "control-plane.db") -> Path:
    config = tmp_path / f"{database_name}.yaml"
    config.write_text(
        "\n".join(
            (
                "control_plane:",
                "  host: 127.0.0.1",
                "  public_url: http://127.0.0.1:8443",
                "database:",
                f"  url: sqlite:///{tmp_path / database_name}",
                "artifacts:",
                f"  directory: {tmp_path / 'artifacts'}",
                "development:",
                "  enabled: true",
                "  allow_insecure_agent_transport: true",
            )
        )
        + "\n",
        encoding="utf-8",
    )
    return config


def test_database_status_check_and_migrate_are_explicit(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config = _config(tmp_path)
    assert control_plane_cli.main(["db", "status", "--config", str(config)]) == 0
    empty = capsys.readouterr().out
    assert "Migration status             empty" in empty

    assert control_plane_cli.main(["db", "check", "--config", str(config)]) == 1
    assert "schema" in capsys.readouterr().err
    assert control_plane_cli.main(["db", "migrate", "--config", str(config)]) == 0
    capsys.readouterr()

    assert control_plane_cli.main(["db", "check", "--config", str(config), "--output", "json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["ready"] is True
    assert payload["current_version"] == SCHEMA_VERSION
    assert payload["minimum_supported_version"] == 11


def test_backup_create_verify_restore_and_upgrade_check(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config = _config(tmp_path)
    assert control_plane_cli.main(["db", "migrate", "--config", str(config)]) == 0
    capsys.readouterr()
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    (artifacts / "operator-note.txt").write_text("backup me\n", encoding="utf-8")
    backup = tmp_path / "phase8-backup.tar.zst"

    assert (
        control_plane_cli.main(
            [
                "backup",
                "create",
                "--config",
                str(config),
                "--destination",
                str(backup),
            ]
        )
        == 0
    )
    assert backup.is_file()
    assert "Backup created" in capsys.readouterr().out

    assert (
        control_plane_cli.main(
            ["backup", "verify", str(backup), "--config", str(config), "--output", "json"]
        )
        == 0
    )
    verification = json.loads(capsys.readouterr().out)
    assert verification["ok"] is True
    assert verification["manifest"]["database_schema"] == SCHEMA_VERSION
    assert verification["manifest"]["artifacts_included"] is True

    (artifacts / "operator-note.txt").write_text("changed\n", encoding="utf-8")
    assert (
        control_plane_cli.main(
            [
                "backup",
                "restore",
                str(backup),
                "--config",
                str(config),
                "--yes",
                "--overwrite",
            ]
        )
        == 0
    )
    assert (artifacts / "operator-note.txt").read_text(encoding="utf-8") == "backup me\n"
    capsys.readouterr()

    assert (
        control_plane_cli.main(
            [
                "upgrade",
                "check",
                str(backup),
                "--config",
                str(config),
                "--output",
                "json",
            ]
        )
        == 0
    )
    report = json.loads(capsys.readouterr().out)
    assert report["ready"] is True
    assert report["checks"][0]["code"] == "DATABASE_SCHEMA"
    assert any(check["code"] == "BACKUP_VERIFIED" for check in report["checks"])


def test_production_serve_fails_before_runtime_when_preflight_is_unsafe(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config = ControlPlaneConfig.model_validate(
        {
            "profile": "production",
            "control_plane": {
                "host": "0.0.0.0",
                "public_url": "https://lab.example.com",
            },
            "database": {"url": "postgresql://lab@database/lab_platform"},
            "artifacts": {"directory": tmp_path / "artifacts"},
            "web": {"public_url": "https://lab.example.com"},
            "proxy": {"enabled": True, "trusted_networks": ["127.0.0.1/32"]},
        }
    )
    report = DiagnosticReport(
        (DiagnosticCheck("secret_key", "error", "application secret is unsafe"),)
    )
    monkeypatch.setattr(control_plane_cli, "load_control_plane_config", lambda _path: config)
    monkeypatch.setattr(control_plane_cli, "collect_diagnostics", lambda *_args, **_kwargs: report)

    class UnexpectedRuntime:
        def __init__(self, _config: ControlPlaneConfig) -> None:
            pytest.fail("runtime must not be constructed after a failed production preflight")

    monkeypatch.setattr(control_plane_cli, "ControlPlaneRuntime", UnexpectedRuntime)

    assert control_plane_cli.main(["serve", "--config", str(tmp_path / "config.yaml")]) == 1
    captured = capsys.readouterr()
    assert "secret_key" in captured.out
    assert "production startup preflight failed" in captured.err


def test_development_serve_does_not_run_production_preflight(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = ControlPlaneConfig.model_validate(
        {
            "control_plane": {
                "host": "127.0.0.1",
                "public_url": "http://127.0.0.1:8443",
            },
            "database": {"url": f"sqlite:///{tmp_path / 'control-plane.db'}"},
            "artifacts": {"directory": tmp_path / "artifacts"},
            "development": {"allow_insecure_agent_transport": True},
        }
    )
    monkeypatch.setattr(control_plane_cli, "load_control_plane_config", lambda _path: config)

    def unexpected_preflight(*_args: object, **_kwargs: object) -> DiagnosticReport:
        pytest.fail("development serve must not run the production preflight")

    monkeypatch.setattr(control_plane_cli, "collect_diagnostics", unexpected_preflight)

    class Runtime:
        def __init__(self, runtime_config: ControlPlaneConfig) -> None:
            self.config = runtime_config

    class Server:
        def __init__(self, runtime: Runtime, *, host: str, port: int) -> None:
            assert runtime.config is config
            assert host == "127.0.0.1"
            assert port == 8443

        def serve_forever(self) -> None:
            return None

        def shutdown(self) -> None:
            return None

    monkeypatch.setattr(control_plane_cli, "ControlPlaneRuntime", Runtime)
    monkeypatch.setattr(control_plane_cli, "ControlPlaneHttpServer", Server)

    assert control_plane_cli.main(["serve", "--config", str(tmp_path / "config.yaml")]) == 0
