from __future__ import annotations

import stat
import subprocess
from pathlib import Path

import pytest
from lab_platform.cli import product


class _Entry:
    def __init__(self, name: str, content: bytes | None = None, children: tuple[_Entry, ...] = ()):
        self.name = name
        self._content = content
        self._children = children

    def is_dir(self) -> bool:
        return self._content is None

    def iterdir(self) -> tuple[_Entry, ...]:
        return self._children

    def read_bytes(self) -> bytes:
        assert self._content is not None
        return self._content


def _template() -> _Entry:
    return _Entry(
        "production",
        children=(
            _Entry("compose.yaml", b"services: {}\n"),
            _Entry(".env.example", b"LAB_RELEASE_CHANNEL=stable\n"),
            _Entry("config", children=(_Entry("control-plane.yaml", b"profile: production\n"),)),
        ),
    )


def test_init_materializes_templates_and_owner_only_secrets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(product, "_deployment_template", lambda _mode: _template())
    destination = tmp_path / "deployment"

    assert product.main(["init", str(destination)]) == 0

    assert (destination / "compose.yaml").read_text() == "services: {}\n"
    assert (destination / ".env").read_text() == "LAB_RELEASE_CHANNEL=stable\n"
    for name in ("control-plane-secret-key", "database-url", "postgres-password"):
        secret = destination / "secrets" / name
        assert secret.read_text().strip()
        assert stat.S_IMODE(secret.stat().st_mode) == 0o600
    assert stat.S_IMODE((destination / "secrets").stat().st_mode) == 0o700
    assert (
        (destination / "secrets" / "database-url")
        .read_text()
        .startswith("postgresql://lab_platform:")
    )
    assert "Initialized production deployment" in capsys.readouterr().out


def test_init_refuses_to_overwrite_existing_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(product, "_deployment_template", lambda _mode: _template())
    destination = tmp_path / "deployment"
    destination.mkdir()
    (destination / "compose.yaml").write_text("mine\n")

    with pytest.raises(FileExistsError, match="Refusing to overwrite"):
        product.main(["init", str(destination)])

    assert (destination / "compose.yaml").read_text() == "mine\n"


def test_dev_uses_packaged_demo_and_structured_subprocess_arguments(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    demo = _Entry(
        "demo",
        children=(
            _Entry("compose.yaml", b"services: {}\n"),
            _Entry(".env.example", b"DEMO=true\n"),
        ),
    )
    monkeypatch.setattr(product, "_deployment_template", lambda _mode: demo)
    observed: dict[str, object] = {}

    class _Completed:
        returncode = 0

    def execute(command: list[str], *, cwd: Path) -> int:
        observed.update(command=command, cwd=cwd)
        return 0

    monkeypatch.setattr(product, "_execute", execute)
    destination = tmp_path / "demo"

    assert product.main(["dev", "up", "--detach", "--directory", str(destination)]) == 0
    assert observed == {
        "command": [
            "docker",
            "compose",
            "-f",
            str(destination / "compose.yaml"),
            "up",
            "--detach",
        ],
        "cwd": destination,
    }


def test_init_rejects_missing_template(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(product, "_deployment_template", lambda _mode: _Entry("empty"))

    with pytest.raises(RuntimeError, match="template is missing"):
        product.main(["init", str(tmp_path / "deployment")])


def test_init_reports_bounded_collision_list(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    template = _Entry(
        "production",
        children=tuple(_Entry(f"file-{index}", b"value\n") for index in range(7)),
    )
    monkeypatch.setattr(product, "_deployment_template", lambda _mode: template)
    destination = tmp_path / "deployment"
    destination.mkdir()
    for index in range(7):
        (destination / f"file-{index}").write_text("mine\n", encoding="utf-8")

    with pytest.raises(FileExistsError, match=r"and 2 more"):
        product.main(["init", str(destination)])


def test_demo_init_without_env_example_creates_empty_env(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    demo = _Entry("demo", children=(_Entry("compose.yaml", b"services: {}\n"),))
    monkeypatch.setattr(product, "_deployment_template", lambda _mode: demo)
    destination = tmp_path / "demo"

    assert product.main(["init", "--demo", str(destination)]) == 0
    assert (destination / ".env").read_bytes() == b""
    assert not (destination / "secrets").exists()


@pytest.mark.parametrize(
    ("arguments", "tail"),
    [
        (["dev", "down"], ["down"]),
        (["dev", "logs", "--follow"], ["logs", "--follow"]),
        (["dev", "ps"], ["ps"]),
    ],
)
def test_dev_actions_build_expected_compose_commands(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    arguments: list[str],
    tail: list[str],
) -> None:
    destination = tmp_path / "demo"
    destination.mkdir()
    (destination / "compose.yaml").write_text("services: {}\n", encoding="utf-8")
    commands: list[list[str]] = []

    def execute(command: list[str], *, cwd: Path) -> int:
        assert cwd == destination
        commands.append(command)
        return 7

    monkeypatch.setattr(product, "_execute", execute)
    assert product.main([*arguments, "--directory", str(destination)]) == 7
    assert commands[0][-len(tail) :] == tail


def test_execute_returns_process_status_and_explains_missing_docker(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "lab_platform.cli.product.subprocess.run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess([], 9),
    )
    assert product._execute(["docker", "compose", "ps"], cwd=tmp_path) == 9

    def missing(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[bytes]:
        raise FileNotFoundError("docker")

    monkeypatch.setattr("lab_platform.cli.product.subprocess.run", missing)
    with pytest.raises(RuntimeError, match="Docker was not found"):
        product._execute(["docker", "compose", "ps"], cwd=tmp_path)


def test_compose_file_supports_legacy_names_and_missing_file(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError, match="No Compose file"):
        product._compose_file(tmp_path)

    legacy = tmp_path / "docker-compose.yaml"
    legacy.write_text("services: {}\n", encoding="utf-8")
    assert product._compose_file(tmp_path) == legacy


def test_parser_requires_a_command() -> None:
    with pytest.raises(SystemExit):
        product.main([])
