from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from lab_platform.control_plane.cli import main
from lab_platform.control_plane.config import load_control_plane_config
from lab_platform.control_plane.runtime import ControlPlaneRuntime
from lab_platform.core.errors import RoleNotAllowedError
from lab_platform.core.identity import IdentityAuthenticationService
from lab_platform.persistence.database import SQLiteDatabase
from lab_platform.persistence.identity import SQLiteIdentityRepository
from lab_platform.persistence.migrations import DEFAULT_ORGANISATION_ID


def _config(
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
    config_path, database_path = _config(tmp_path)
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
    config_path, database_path = _config(tmp_path)
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
    config_path, _database_path = _config(tmp_path)
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
    config_path, database_path = _config(
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
