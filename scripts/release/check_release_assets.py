#!/usr/bin/env python3
"""Verify release checksums and the minimum downloadable artifact set."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path


def verify_release_directory(
    directory: Path,
    version: str,
    python_version: str,
) -> list[str]:
    errors: list[str] = []
    checksum_path = directory / "SHA256SUMS"
    if not checksum_path.is_file():
        return ["SHA256SUMS is missing"]
    entries = _read_checksums(checksum_path, errors)
    web_archive = f"lab-platform-web-{version}.tar.gz"
    source_distribution = f"lab_platform-{python_version}.tar.gz"
    required_artifacts = (
        web_archive,
        source_distribution,
        "source.spdx.json",
        "control-plane.spdx.json",
        "agent.spdx.json",
    )
    names = set(entries)
    for required in required_artifacts:
        if required not in names:
            errors.append(f"release artifact is missing: {required}")
    wheel_prefix = f"lab_platform-{python_version}-"
    if not any(name.startswith(wheel_prefix) and name.endswith(".whl") for name in names):
        errors.append(f"release has no wheel for Python version {python_version}")
    for name, expected in entries.items():
        path = directory / name
        if not path.is_file():
            errors.append(f"checksummed artifact is missing: {name}")
            continue
        actual = hashlib.sha256(path.read_bytes()).hexdigest()
        if actual != expected:
            errors.append(f"checksum mismatch for {name}")
    signature_bundle = directory / "SHA256SUMS.sigstore.json"
    if not signature_bundle.is_file():
        errors.append("SHA256SUMS.sigstore.json is missing")
    else:
        try:
            json.loads(signature_bundle.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            errors.append(f"invalid checksum signature bundle: {exc}")
    for name in ("source.spdx.json", "control-plane.spdx.json", "agent.spdx.json"):
        path = directory / name
        if path.is_file():
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError) as exc:
                errors.append(f"invalid SBOM {name}: {exc}")
                continue
            if not str(payload.get("spdxVersion", "")).startswith("SPDX-"):
                errors.append(f"SBOM {name} does not declare an SPDX version")
    return errors


def _read_checksums(path: Path, errors: list[str]) -> dict[str, str]:
    entries: dict[str, str] = {}
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        fields = line.split(maxsplit=1)
        if len(fields) != 2 or len(fields[0]) != 64:
            errors.append(f"invalid SHA256SUMS line {number}")
            continue
        digest, name = fields[0].casefold(), fields[1].lstrip("*")
        if any(character not in "0123456789abcdef" for character in digest):
            errors.append(f"invalid SHA-256 digest on line {number}")
            continue
        if Path(name).name != name or name in entries:
            errors.append(f"unsafe or duplicate checksum filename on line {number}")
            continue
        entries[name] = digest
    return entries


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    parser.add_argument("--version", required=True)
    parser.add_argument("--python-version", required=True)
    args = parser.parse_args()
    errors = verify_release_directory(args.directory, args.version, args.python_version)
    if errors:
        for error in errors:
            print(f"error: {error}", file=sys.stderr)
        return 1
    print(f"Verified release assets in {args.directory}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
