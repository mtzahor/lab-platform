from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from lab_platform.control_plane.cli import main
from lab_platform.core.errors import RoleNotAllowedError
from lab_platform.core.identity import IdentityAuthenticationService
from lab_platform.persistence.database import SQLiteDatabase
from lab_platform.persistence.identity import SQLiteIdentityRepository


def _config(tmp_path: Path) -> tuple[Path, Path]:
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
                "development:",
                "  enabled: true",
                "  allow_insecure_agent_transport: true",
            )
        ),
        encoding="utf-8",
    )
    return config_path, database_path


def _login(database_path: Path, password: str) -> str:
    async def scenario() -> str:
        database = SQLiteDatabase(database_path)
        database.initialize()
        repository = SQLiteIdentityRepository(database)
        service = IdentityAuthenticationService(repository)
        try:
            issued = await service.login(
                organisation_slug="default",
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
