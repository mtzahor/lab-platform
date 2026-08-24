#!/usr/bin/env python3
"""Fail if a community source tree or wheel imports commercial-only modules."""

from __future__ import annotations

import argparse
import ast
import re
import sys
import tomllib
import zipfile
from pathlib import Path, PurePosixPath

_FORBIDDEN_PREFIXES = (
    "lab_platform.enterprise",
    "lab_platform.commercial",
    "lab_platform_enterprise",
    "lab_platform_commercial",
)
_FORBIDDEN_PATH_PARTS = {
    "commercial",
    "enterprise",
    "lab_platform_commercial",
    "lab_platform_enterprise",
}


def check_source(root: Path) -> list[str]:
    errors: list[str] = []
    for path in sorted((*root.glob("apps/**/*.py"), *root.glob("packages/**/*.py"))):
        relative_path = path.relative_to(root)
        if any(part.casefold() in _FORBIDDEN_PATH_PARTS for part in relative_path.parts):
            errors.append(f"{relative_path}: commercial namespace is present in community source")
        errors.extend(_check_python(path.read_text(encoding="utf-8"), str(relative_path)))
    project_path = root / "pyproject.toml"
    if project_path.is_file():
        project = tomllib.loads(project_path.read_text(encoding="utf-8"))["project"]
        dependencies = [*project.get("dependencies", [])]
        for optional in project.get("optional-dependencies", {}).values():
            dependencies.extend(optional)
        for dependency in dependencies:
            name = re.split(r"[\s\[<>=!~]", str(dependency).strip(), maxsplit=1)[0]
            normalized = name.replace("_", "-").casefold()
            if normalized in {"lab-platform-enterprise", "lab-platform-commercial"}:
                errors.append(f"pyproject.toml: community package requires {name}")
    return errors


def check_wheel(path: Path) -> list[str]:
    errors: list[str] = []
    with zipfile.ZipFile(path) as wheel:
        for name in wheel.namelist():
            parts = PurePosixPath(name).parts
            if any(part.casefold() in _FORBIDDEN_PATH_PARTS for part in parts):
                errors.append(f"{name}: commercial namespace is present in the community wheel")
            if name.endswith(".py"):
                source = wheel.read(name).decode("utf-8")
                errors.extend(_check_python(source, f"{path.name}:{name}"))
        metadata_names = [name for name in wheel.namelist() if name.endswith(".dist-info/METADATA")]
        for name in metadata_names:
            metadata = wheel.read(name).decode("utf-8").casefold()
            if any(
                f"requires-dist: lab-platform-{edition}" in metadata
                for edition in ("enterprise", "commercial")
            ):
                errors.append(f"{path.name}:{name}: community wheel requires commercial package")
    return errors


def _check_python(source: str, label: str) -> list[str]:
    tree = ast.parse(source, filename=label)
    errors: list[str] = []
    for node in ast.walk(tree):
        modules: tuple[str, ...] = ()
        if isinstance(node, ast.Import):
            modules = tuple(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            base = node.module or ""
            imported = tuple(
                f"{base}.{alias.name}".lstrip(".") for alias in node.names if alias.name != "*"
            )
            modules = ((base,) if base else ()) + imported
        for module in modules:
            fully_qualified = any(
                module == prefix or module.startswith(prefix + ".")
                for prefix in _FORBIDDEN_PREFIXES
            )
            relative_commercial = (
                isinstance(node, ast.ImportFrom)
                and node.level > 0
                and module.partition(".")[0].casefold() in {"enterprise", "commercial"}
            )
            if fully_qualified or relative_commercial:
                line = getattr(node, "lineno", 0)
                errors.append(f"{label}:{line}: forbidden commercial import {module}")
                break
    return errors


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[2])
    parser.add_argument("--wheel", type=Path, action="append", default=[])
    args = parser.parse_args()
    errors = check_source(args.root.resolve())
    for wheel in args.wheel:
        errors.extend(check_wheel(wheel))
    if errors:
        print("Open-core boundary validation failed:", file=sys.stderr)
        for error in errors:
            print(f"- {error}", file=sys.stderr)
        return 1
    print("Community source and wheel contain no commercial-module imports.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
