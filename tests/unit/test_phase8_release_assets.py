from __future__ import annotations

import hashlib
import json
import re
import zipfile
from pathlib import Path

import pytest

from scripts.release import check_oss_boundary, check_release_assets, metadata


@pytest.mark.parametrize(
    ("semantic", "python"),
    [
        ("0.9.0", "0.9.0"),
        ("0.9.0-alpha", "0.9.0a0"),
        ("0.9.0-beta.1", "0.9.0b1"),
        ("1.0.0-rc.2", "1.0.0rc2"),
    ],
)
def test_release_semver_has_one_canonical_python_mapping(semantic: str, python: str) -> None:
    assert metadata.semver_to_pep440(semantic) == python


@pytest.mark.parametrize("version", ["0.9", "v0.9.0", "0.9.0-preview.1", "0.9.0-beta.01"])
def test_release_semver_rejects_ambiguous_python_versions(version: str) -> None:
    with pytest.raises(ValueError):
        metadata.semver_to_pep440(version)


def test_release_tag_selects_stable_and_preview_channels(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    versions = {
        "pyproject.toml": "0.9.0b1",
        "packages/core/src/lab_platform/core/version.py": "0.9.0-beta.1",
        "apps/web/package.json": "0.9.0-beta.1",
        "apps/web/package-lock.json": "0.9.0-beta.1",
        'apps/web/package-lock.json packages[""]': "0.9.0-beta.1",
        "deploy/production/.env.example LAB_VERSION": "0.9.0-beta.1",
    }
    monkeypatch.setattr(metadata, "repository_versions", lambda _root: versions)
    monkeypatch.setattr(metadata, "commit_build_date", lambda _root: "2026-08-23T00:00:00Z")

    preview = metadata.parse_release_tag("v0.9.0-beta.1", root=tmp_path)
    assert preview.channel == "preview"
    assert preview.prerelease is True

    stable_versions = {
        name: value.replace("0.9.0b1", "0.9.0").replace("-beta.1", "")
        for name, value in versions.items()
    }
    monkeypatch.setattr(metadata, "repository_versions", lambda _root: stable_versions)
    stable = metadata.parse_release_tag("v0.9.0", root=tmp_path)
    assert stable.channel == "stable"
    assert stable.prerelease is False


def test_release_asset_verifier_checks_digests_and_spdx(tmp_path: Path) -> None:
    payloads = {
        "lab_platform-0.9.0b1-py3-none-any.whl": b"wheel",
        "lab_platform-0.9.0b1.tar.gz": b"source",
        "lab-platform-web-0.9.0-beta.1.tar.gz": b"web",
        "source.spdx.json": json.dumps({"spdxVersion": "SPDX-2.3"}).encode(),
        "control-plane.spdx.json": json.dumps({"spdxVersion": "SPDX-2.3"}).encode(),
        "agent.spdx.json": json.dumps({"spdxVersion": "SPDX-2.3"}).encode(),
    }
    lines: list[str] = []
    for name, content in payloads.items():
        (tmp_path / name).write_bytes(content)
        lines.append(f"{hashlib.sha256(content).hexdigest()}  {name}")
    (tmp_path / "SHA256SUMS").write_text("\n".join(lines) + "\n", encoding="utf-8")
    (tmp_path / "SHA256SUMS.sigstore.json").write_text("{}\n", encoding="utf-8")

    assert check_release_assets.verify_release_directory(tmp_path, "0.9.0-beta.1", "0.9.0b1") == []
    (tmp_path / "agent.spdx.json").write_text("{}", encoding="utf-8")
    assert "checksum mismatch for agent.spdx.json" in check_release_assets.verify_release_directory(
        tmp_path,
        "0.9.0-beta.1",
        "0.9.0b1",
    )


def test_release_asset_verifier_requires_sdist_sbom_and_signature(tmp_path: Path) -> None:
    web_name = "lab-platform-web-0.9.0-beta.1.tar.gz"
    payloads = {
        "lab_platform-0.9.0b1-py3-none-any.whl": b"wheel",
        web_name: b"web",
        "control-plane.spdx.json": json.dumps({"spdxVersion": "SPDX-2.3"}).encode(),
        "agent.spdx.json": json.dumps({"spdxVersion": "SPDX-2.3"}).encode(),
    }
    (tmp_path / "SHA256SUMS").write_text(
        "\n".join(
            f"{hashlib.sha256(content).hexdigest()}  {name}" for name, content in payloads.items()
        )
        + "\n",
        encoding="utf-8",
    )
    for name, content in payloads.items():
        (tmp_path / name).write_bytes(content)

    errors = check_release_assets.verify_release_directory(
        tmp_path,
        "0.9.0-beta.1",
        "0.9.0b1",
    )

    assert "release artifact is missing: lab_platform-0.9.0b1.tar.gz" in errors
    assert "release artifact is missing: source.spdx.json" in errors
    assert "SHA256SUMS.sigstore.json is missing" in errors


def test_oss_boundary_rejects_source_and_wheel_commercial_imports(tmp_path: Path) -> None:
    source_root = tmp_path / "source"
    package = source_root / "packages" / "core"
    package.mkdir(parents=True)
    (package / "safe.py").write_text("from lab_platform.core import VERSION\n", encoding="utf-8")
    assert check_oss_boundary.check_source(source_root) == []
    (package / "unsafe.py").write_text("import lab_platform.enterprise.sso\n", encoding="utf-8")
    assert "forbidden commercial import" in check_oss_boundary.check_source(source_root)[0]

    wheel = tmp_path / "community.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr("lab_platform/community.py", "import lab_platform_enterprise\n")
        archive.writestr("community-1.0.dist-info/METADATA", "Name: community\n")
    assert "forbidden commercial import" in check_oss_boundary.check_wheel(wheel)[0]


def test_oss_boundary_rejects_relative_imports_namespaces_and_dependencies(
    tmp_path: Path,
) -> None:
    package = tmp_path / "apps/community/lab_platform/community"
    package.mkdir(parents=True)
    (package / "unsafe.py").write_text(
        "from lab_platform import enterprise\nfrom .commercial import policy\n",
        encoding="utf-8",
    )
    commercial = tmp_path / "packages/commercial"
    commercial.mkdir(parents=True)
    (commercial / "module.py").write_text("VALUE = True\n", encoding="utf-8")
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "community"\nversion = "1.0"\n'
        'dependencies = ["lab_platform_enterprise>=1"]\n',
        encoding="utf-8",
    )

    errors = check_oss_boundary.check_source(tmp_path)

    assert sum("forbidden commercial import" in error for error in errors) == 2
    assert any("commercial namespace" in error for error in errors)
    assert any("community package requires lab_platform_enterprise" in error for error in errors)


def test_packaged_deployment_templates_track_inspectable_source_templates() -> None:
    root = Path(__file__).resolve().parents[2]
    package_root = root / "apps/cli/src/lab_platform/cli/deployment"
    production_files = {
        "compose.yaml",
        "control-plane.yaml",
        "Caddyfile",
        ".env.example",
        "README.md",
    }
    demo_files = {"compose.yaml", "control-plane.yaml", "Caddyfile", "README.md"}

    assert {path.name for path in (package_root / "production").iterdir()} == production_files
    assert {path.name for path in (package_root / "demo").iterdir()} == demo_files
    for name in production_files:
        assert (package_root / "production" / name).read_bytes() == (
            root / "deploy/production" / name
        ).read_bytes()
    for name in demo_files - {"compose.yaml"}:
        assert (package_root / "demo" / name).read_bytes() == (
            root / "deploy/demo" / name
        ).read_bytes()

    source_demo = (root / "deploy/demo/compose.yaml").read_text(encoding="utf-8")
    expected_packaged_demo = re.sub(
        r"    build:\n      context: \.\./\.\.\n"
        r"      dockerfile: docker/(?:control-plane|agent)/Dockerfile\n",
        "",
        source_demo,
    )
    assert (package_root / "demo/compose.yaml").read_text(encoding="utf-8") == (
        expected_packaged_demo
    )
    assert "LAB_HEALTHCHECK_URL: http://127.0.0.1:8081/api/v1/health" in source_demo


def test_demo_container_prepares_shared_non_root_state_directory() -> None:
    root = Path(__file__).resolve().parents[2]
    control_plane = (root / "docker/control-plane/Dockerfile").read_text(encoding="utf-8")
    agent = (root / "docker/agent/Dockerfile").read_text(encoding="utf-8")

    assert "/run/lab-platform-demo" in control_plane
    for dockerfile in (control_plane, agent):
        assert "USER 10001:10001" in dockerfile
        assert "COPY pyproject.toml uv.lock README.md LICENSE NOTICE ./" in dockerfile


def test_production_template_pins_the_matching_release_image() -> None:
    root = Path(__file__).resolve().parents[2]
    product_version = metadata.repository_versions(root)[
        "packages/core/src/lab_platform/core/version.py"
    ]
    env_lines = (root / "deploy/production/.env.example").read_text(encoding="utf-8").splitlines()
    compose = (root / "deploy/production/compose.yaml").read_text(encoding="utf-8")

    assert f"LAB_VERSION={product_version}" in env_lines
    assert f"${{LAB_VERSION:-{product_version}}}" in compose
    assert "${LAB_VERSION:-stable}" not in compose
