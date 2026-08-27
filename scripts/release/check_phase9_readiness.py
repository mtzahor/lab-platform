#!/usr/bin/env python3
"""Validate Phase 9 readiness/compatibility records and enforce the 1.0 release gate."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

CRITERION_IDS = set(range(1, 61))
CRITERION_STATES = {
    "pending",
    "partial",
    "evidence_available",
    "external_evidence_required",
    "passed",
}
COMPATIBILITY_STATUSES = {
    "VERIFIED",
    "SUPPORTED",
    "EXPERIMENTAL",
    "COMMUNITY",
    "DEPRECATED",
}


@dataclass(frozen=True, slots=True)
class ReadinessResult:
    errors: tuple[str, ...]
    incomplete: tuple[int, ...]

    @property
    def releasable(self) -> bool:
        return not self.errors and not self.incomplete


def _mapping(value: object, location: str, errors: list[str]) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        errors.append(f"{location} must be a mapping")
        return {}
    return value


def _read_yaml(path: Path, errors: list[str]) -> Mapping[str, Any]:
    try:
        return _mapping(yaml.safe_load(path.read_text(encoding="utf-8")), str(path), errors)
    except (OSError, yaml.YAMLError) as exc:
        errors.append(f"could not read {path}: {exc}")
        return {}


def validate_readiness(root: Path, path: Path) -> ReadinessResult:
    errors: list[str] = []
    document = _read_yaml(path, errors)
    if document.get("schema_version") != 1:
        errors.append("readiness schema_version must be 1")
    if document.get("phase") != 9:
        errors.append("readiness phase must be 9")
    if document.get("target_release") != "1.0.0":
        errors.append("readiness target_release must be 1.0.0")
    criteria = document.get("criteria")
    if not isinstance(criteria, list):
        errors.append("readiness criteria must be a list")
        criteria = []
    seen: set[int] = set()
    incomplete: list[int] = []
    for index, raw in enumerate(criteria):
        criterion = _mapping(raw, f"criteria[{index}]", errors)
        criterion_id = criterion.get("id")
        if not isinstance(criterion_id, int) or isinstance(criterion_id, bool):
            errors.append(f"criteria[{index}].id must be an integer")
            continue
        if criterion_id in seen:
            errors.append(f"criterion ID {criterion_id} is duplicated")
        seen.add(criterion_id)
        requirement = criterion.get("requirement")
        if not isinstance(requirement, str) or not requirement.strip():
            errors.append(f"criterion {criterion_id} has no requirement")
        gate = criterion.get("gate")
        if not isinstance(gate, str) or not gate.strip():
            errors.append(f"criterion {criterion_id} has no gate")
        state = criterion.get("state")
        if state not in CRITERION_STATES:
            errors.append(f"criterion {criterion_id} has invalid state {state!r}")
        if state != "passed":
            incomplete.append(criterion_id)
        evidence = criterion.get("evidence", [])
        if not isinstance(evidence, list) or not all(isinstance(item, str) for item in evidence):
            errors.append(f"criterion {criterion_id} evidence must be a list of paths")
            continue
        for item in evidence:
            evidence_path = root / item
            if not evidence_path.exists():
                errors.append(f"criterion {criterion_id} evidence does not exist: {item}")
    missing = sorted(CRITERION_IDS.difference(seen))
    unexpected = sorted(seen.difference(CRITERION_IDS))
    if missing:
        errors.append(f"readiness criteria are missing IDs: {missing}")
    if unexpected:
        errors.append(f"readiness criteria contain unexpected IDs: {unexpected}")
    expected_overall = "READY" if not incomplete and not errors else "NOT_READY"
    if document.get("overall_status") != expected_overall:
        errors.append(
            f"overall_status must be {expected_overall} for the recorded criterion states"
        )
    return ReadinessResult(tuple(errors), tuple(sorted(incomplete)))


def validate_compatibility(root: Path, path: Path) -> tuple[str, ...]:
    errors: list[str] = []
    document = _read_yaml(path, errors)
    if document.get("schema_version") != 1:
        errors.append("compatibility schema_version must be 1")
    definitions = _mapping(document.get("status_definitions"), "status_definitions", errors)
    if set(definitions) != COMPATIBILITY_STATUSES:
        errors.append("compatibility status_definitions must define the five public statuses")
    for group_name in ("plugins", "accessories"):
        group = _mapping(document.get(group_name), group_name, errors)
        for name, raw_entry in group.items():
            entry = _mapping(raw_entry, f"{group_name}.{name}", errors)
            _validate_compatibility_entry(
                root,
                f"{group_name}.{name}",
                entry,
                errors,
            )
            tested = entry.get("tested", [])
            if not isinstance(tested, list):
                errors.append(f"{group_name}.{name}.tested must be a list")
                continue
            for index, raw_test in enumerate(tested):
                test = _mapping(raw_test, f"{group_name}.{name}.tested[{index}]", errors)
                _validate_compatibility_entry(
                    root,
                    f"{group_name}.{name}.tested[{index}]",
                    test,
                    errors,
                    require_implementation=False,
                )
    return tuple(errors)


def _validate_compatibility_entry(
    root: Path,
    location: str,
    entry: Mapping[str, Any],
    errors: list[str],
    *,
    require_implementation: bool = True,
) -> None:
    status = entry.get("status")
    if status not in COMPATIBILITY_STATUSES:
        errors.append(f"{location}.status has invalid value {status!r}")
        return
    if require_implementation and not entry.get("implementation_state"):
        errors.append(f"{location}.implementation_state is required")
    physical_evidence = entry.get("latest_physical_evidence")
    if status == "VERIFIED" and not physical_evidence:
        errors.append(f"{location} is VERIFIED without latest_physical_evidence")
    if status == "DEPRECATED":
        for field in ("replacement", "deprecated_since", "removal_release"):
            if not entry.get(field):
                errors.append(f"{location}.{field} is required for DEPRECATED status")
    for field in ("automated_contract", "latest_physical_evidence"):
        value = entry.get(field)
        if (
            isinstance(value, str)
            and not value.startswith(("https://", "http://"))
            and not (root / value).exists()
        ):
            errors.append(f"{location}.{field} does not exist: {value}")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[2])
    parser.add_argument("--readiness", type=Path)
    parser.add_argument("--compatibility", type=Path)
    parser.add_argument(
        "--allow-incomplete",
        action="store_true",
        help="Validate record structure without approving the 1.0 release gate.",
    )
    args = parser.parse_args(argv)
    root = args.root.resolve()
    readiness_path = args.readiness or root / "release/phase9-readiness.yaml"
    compatibility_path = args.compatibility or root / "compatibility/hardware.yaml"
    readiness = validate_readiness(root, readiness_path)
    errors = [*readiness.errors, *validate_compatibility(root, compatibility_path)]
    if errors:
        for error in errors:
            print(f"error: {error}", file=sys.stderr)
        return 2
    if readiness.incomplete and not args.allow_incomplete:
        print(
            "error: Phase 9 is not ready for 1.0; incomplete criteria: "
            + ", ".join(str(item) for item in readiness.incomplete),
            file=sys.stderr,
        )
        return 1
    if readiness.incomplete:
        print(f"Phase 9 records are valid; {len(readiness.incomplete)} release gates remain open.")
    else:
        print("Phase 9 records are valid and all 1.0 release gates are recorded as passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
