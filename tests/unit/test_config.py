from __future__ import annotations

from pathlib import Path

import pytest
from lab_platform.config import load_config, validate_config
from pydantic import ValidationError


def test_missing_files_use_defaults(tmp_path: Path) -> None:
    config = load_config(tmp_path)

    assert config.agent.name == "local-agent"
    assert config.agent.port == 8080
    assert config.simlab.benches == 5
    assert config.plugins == ["power", "serial", "firmware"]


def test_files_are_deep_merged_and_validated(tmp_path: Path) -> None:
    (tmp_path / "agent.yaml").write_text(
        "agent:\n  name: test-agent\n  port: 9090\nplugins:\n  - power\n",
        encoding="utf-8",
    )
    (tmp_path / "simlab.yaml").write_text(
        "agent:\n  log_level: DEBUG\nsimlab:\n  benches: 2\n",
        encoding="utf-8",
    )

    config = validate_config(tmp_path)

    assert config.agent.name == "test-agent"
    assert config.agent.port == 9090
    assert config.agent.log_level == "DEBUG"
    assert config.simlab.benches == 2
    assert config.plugins == ["power"]


@pytest.mark.parametrize(
    "contents, exception",
    [
        ("- not-a-mapping\n", ValueError),
        ("agent: [unterminated\n", ValueError),
        ("agent:\n  unexpected: true\n", ValidationError),
        ("agent:\n  port: 70000\n", ValidationError),
    ],
)
def test_invalid_configuration_fails(
    tmp_path: Path,
    contents: str,
    exception: type[Exception],
) -> None:
    (tmp_path / "agent.yaml").write_text(contents, encoding="utf-8")

    with pytest.raises(exception):
        load_config(tmp_path)


def test_empty_yaml_file_uses_defaults(tmp_path: Path) -> None:
    (tmp_path / "agent.yaml").write_text("", encoding="utf-8")

    assert load_config(tmp_path).agent.name == "local-agent"
