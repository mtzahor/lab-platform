from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from lab_platform.control_plane import cli as control_plane_cli
from lab_platform.control_plane.cli import main
from lab_platform.control_plane.config import ControlPlaneConfig, load_control_plane_config
from lab_platform.control_plane.diagnostics import DiagnosticCheck, DiagnosticReport
from lab_platform.control_plane.runtime import ControlPlaneRuntime
from lab_platform.core.errors import RoleNotAllowedError
from lab_platform.core.identity import IdentityAuthenticationService
from lab_platform.persistence import SCHEMA_VERSION
from lab_platform.persistence.database import SQLiteDatabase
from lab_platform.persistence.identity import SQLiteIdentityRepository
from lab_platform.persistence.migrations import DEFAULT_ORGANISATION_ID


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


def _config_bootstrap_cli(
    tmp_path: Path,
    *,
    organisation_slug: str = "default",
    organisation_name: str = "Default Organisation",
) -> tuple[Path, Path]:
    database_path = tmp_path / "control-plane.db"
    config_path = tmp_path / "control-plane.yaml"
    config_path.write_text(
        "\n".join(
            (
                "control_plane:",
                "  host: 127.0.0.1",
                "  public_url: http://127.0.0.1:8443",
                "database:",
                f"  url: sqlite:///{database_path}",
                "artifacts:",
                f"  directory: {tmp_path / 'artifacts'}",
                "identity:",
                f"  default_organisation_slug: {organisation_slug}",
                f"  default_organisation_name: {organisation_name}",
                "development:",
                "  enabled: true",
                "  allow_insecure_agent_transport: true",
            )
        ),
        encoding="utf-8",
    )
    return config_path, database_path


def _login(database_path: Path, password: str, *, organisation_slug: str = "default") -> str:
    async def scenario() -> str:
        database = SQLiteDatabase(database_path)
        database.initialize()
        repository = SQLiteIdentityRepository(database)
        service = IdentityAuthenticationService(repository)
        try:
            issued = await service.login(
                organisation_slug=organisation_slug,
                username="michael",
                password=password,
            )
            return issued.principal.display_name
        finally:
            database.close()

    return asyncio.run(scenario())


def test_bootstrap_admin_prompts_without_exposing_password(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config_path, database_path = _config_bootstrap_cli(tmp_path)
    answers = iter(("correct horse battery staple", "correct horse battery staple"))
    monkeypatch.setattr("getpass.getpass", lambda _prompt: next(answers))

    result = main(
        [
            "bootstrap-admin",
            "--config",
            str(config_path),
            "--username",
            "michael",
            "--display-name",
            "Michael Example",
        ]
    )

    assert result == 0
    output = capsys.readouterr()
    assert "Bootstrap administrator created: michael" in output.out
    assert "correct horse" not in output.out + output.err
    assert _login(database_path, "correct horse battery staple") == "Michael Example"


def test_bootstrap_requires_recovery_and_environment_password_is_not_printed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config_path, database_path = _config_bootstrap_cli(tmp_path)
    monkeypatch.setenv("BOOTSTRAP_PASSWORD", "correct horse battery staple")
    arguments = [
        "bootstrap-admin",
        "--config",
        str(config_path),
        "--username",
        "michael",
        "--display-name",
        "Michael Example",
        "--password-env",
        "BOOTSTRAP_PASSWORD",
    ]
    assert main(arguments) == 0
    capsys.readouterr()

    with pytest.raises(RoleNotAllowedError, match="owner already exists"):
        main(arguments)

    monkeypatch.setenv("BOOTSTRAP_PASSWORD", "replacement horse battery password")
    assert main([*arguments, "--recovery"]) == 0
    output = capsys.readouterr()
    assert "Bootstrap administrator recovered: michael" in output.out
    assert "replacement horse" not in output.out + output.err
    assert _login(database_path, "replacement horse battery password") == "Michael Example"


def test_bootstrap_does_not_accept_plaintext_password_argument(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config_path, _database_path = _config_bootstrap_cli(tmp_path)
    with pytest.raises(SystemExit):
        main(
            [
                "bootstrap-admin",
                "--config",
                str(config_path),
                "--username",
                "michael",
                "--display-name",
                "Michael",
                "--password",
                "unsafe",
            ]
        )
    assert "unrecognized arguments: --password" in capsys.readouterr().err


def test_bootstrap_reuses_configured_default_organisation_on_runtime_start(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path, database_path = _config_bootstrap_cli(
        tmp_path,
        organisation_slug="demo",
        organisation_name="Deployment acceptance",
    )
    monkeypatch.setenv("BOOTSTRAP_PASSWORD", "correct horse battery staple")

    assert (
        main(
            [
                "bootstrap-admin",
                "--config",
                str(config_path),
                "--username",
                "michael",
                "--display-name",
                "Michael Example",
                "--password-env",
                "BOOTSTRAP_PASSWORD",
            ]
        )
        == 0
    )

    async def scenario() -> None:
        runtime = ControlPlaneRuntime(load_control_plane_config(config_path))
        await runtime.start()
        try:
            with runtime.database.transaction() as connection:
                rows = connection.execute("SELECT id, slug, name FROM organisations").fetchall()
            assert [tuple(row) for row in rows] == [
                (DEFAULT_ORGANISATION_ID, "demo", "Deployment acceptance")
            ]
        finally:
            await runtime.stop()

    asyncio.run(scenario())
    assert (
        _login(
            database_path,
            "correct horse battery staple",
            organisation_slug="demo",
        )
        == "Michael Example"
    )
