#!/usr/bin/env python3
"""Validate a release tag and emit deterministic release metadata."""

from __future__ import annotations

import argparse
import ast
import json
import re
import subprocess
import tomllib
from dataclasses import asdict, dataclass
from pathlib import Path

_SEMVER_PATTERN = re.compile(
    r"^(?P<major>0|[1-9]\d*)\."
    r"(?P<minor>0|[1-9]\d*)\."
    r"(?P<patch>0|[1-9]\d*)"
    r"(?:-(?P<prerelease>[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?"
    r"(?:\+(?P<build>[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?$"
)


@dataclass(frozen=True, slots=True)
class ReleaseMetadata:
    version: str
    python_version: str
    channel: str
    prerelease: bool
    mutable_tag: str
    build_date: str


def parse_release_tag(tag: str, *, root: Path) -> ReleaseMetadata:
    version = tag.removeprefix("v")
    match = _SEMVER_PATTERN.fullmatch(version)
    if match is None or not tag.startswith("v"):
        raise ValueError("release tags must use v<SemVer>, for example v0.9.0-beta.1")
    if match.group("build") is not None:
        raise ValueError("release tags must not contain mutable SemVer build metadata")

    prerelease = match.group("prerelease") is not None
    channel = "preview" if prerelease else "stable"
    expected_python = semver_to_pep440(version)
    versions = repository_versions(root)
    expected = {
        "pyproject.toml": expected_python,
        "packages/core/src/lab_platform/core/version.py": version,
        "apps/web/package.json": version,
        "apps/web/package-lock.json": version,
        'apps/web/package-lock.json packages[""]': version,
        "deploy/production/.env.example LAB_VERSION": version,
    }
    mismatches = [
        f"{name}: expected {expected[name]!r}, found {versions[name]!r}"
        for name in expected
        if versions[name] != expected[name]
    ]
    if mismatches:
        raise ValueError("release version mismatch:\n" + "\n".join(mismatches))

    return ReleaseMetadata(
        version=version,
        python_version=expected_python,
        channel=channel,
        prerelease=prerelease,
        mutable_tag=channel,
        build_date=commit_build_date(root),
    )


def semver_to_pep440(version: str) -> str:
    match = _SEMVER_PATTERN.fullmatch(version)
    if match is None:
        raise ValueError(f"invalid semantic version: {version}")
    base = f"{match.group('major')}.{match.group('minor')}.{match.group('patch')}"
    prerelease = match.group("prerelease")
    if prerelease is None:
        return base
    parts = prerelease.split(".")
    label = parts[0].casefold()
    aliases = {"alpha": "a", "a": "a", "beta": "b", "b": "b", "rc": "rc"}
    if label not in aliases or len(parts) > 2:
        raise ValueError(
            "Python releases support SemVer prereleases alpha[.N], beta[.N], or rc[.N]"
        )
    number = 0 if len(parts) == 1 else _numeric_identifier(parts[1])
    return f"{base}{aliases[label]}{number}"


def repository_versions(root: Path) -> dict[str, str]:
    project = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    web = json.loads((root / "apps/web/package.json").read_text(encoding="utf-8"))
    web_lock = json.loads((root / "apps/web/package-lock.json").read_text(encoding="utf-8"))
    deployment_environment = (root / "deploy/production/.env.example").read_text(encoding="utf-8")
    version_module = ast.parse(
        (root / "packages/core/src/lab_platform/core/version.py").read_text(encoding="utf-8")
    )
    product_version: str | None = None
    for statement in version_module.body:
        if isinstance(statement, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == "VERSION" for target in statement.targets
        ):
            value = ast.literal_eval(statement.value)
            if isinstance(value, str):
                product_version = value
                break
    if product_version is None:
        raise ValueError("packages/core version.py must contain a literal VERSION assignment")
    return {
        "pyproject.toml": str(project["project"]["version"]),
        "packages/core/src/lab_platform/core/version.py": product_version,
        "apps/web/package.json": str(web["version"]),
        "apps/web/package-lock.json": str(web_lock["version"]),
        'apps/web/package-lock.json packages[""]': str(web_lock["packages"][""]["version"]),
        "deploy/production/.env.example LAB_VERSION": _dotenv_value(
            deployment_environment,
            "LAB_VERSION",
        ),
    }


def _dotenv_value(content: str, name: str) -> str:
    prefix = name + "="
    values = [line.removeprefix(prefix) for line in content.splitlines() if line.startswith(prefix)]
    if len(values) != 1 or not values[0]:
        raise ValueError(f"deployment environment must define {name} exactly once")
    return values[0]


def commit_build_date(root: Path) -> str:
    completed = subprocess.run(
        ["git", "show", "-s", "--format=%cI", "HEAD"],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    )
    value = completed.stdout.strip()
    if value.endswith("+00:00"):
        return value[:-6] + "Z"
    return value


def write_github_output(path: Path, metadata: ReleaseMetadata) -> None:
    values = {
        "version": metadata.version,
        "python_version": metadata.python_version,
        "channel": metadata.channel,
        "prerelease": str(metadata.prerelease).lower(),
        "mutable_tag": metadata.mutable_tag,
        "build_date": metadata.build_date,
    }
    with path.open("a", encoding="utf-8") as output:
        for key, value in values.items():
            if "\n" in value or "\r" in value:
                raise ValueError(f"release output {key} contains a newline")
            output.write(f"{key}={value}\n")


def _numeric_identifier(value: str) -> int:
    if not value.isdigit() or (len(value) > 1 and value.startswith("0")):
        raise ValueError("prerelease numbers must be canonical non-negative integers")
    return int(value)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tag", required=True)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[2])
    parser.add_argument("--github-output", type=Path)
    args = parser.parse_args()
    metadata = parse_release_tag(args.tag, root=args.root.resolve())
    if args.github_output is not None:
        write_github_output(args.github_output, metadata)
    else:
        print(json.dumps(asdict(metadata), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
