from __future__ import annotations

import os
import re
from dataclasses import asdict, dataclass
from typing import Literal, cast

from lab_platform.core.version import VERSION

API_VERSION = "v1"
PLUGIN_API_VERSION = "1.0"
ReleaseChannel = Literal["stable", "preview", "nightly"]
_COMMIT_PATTERN = re.compile(r"^[0-9a-fA-F]{7,64}$")
_BUILD_DATE_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z$")


@dataclass(frozen=True, slots=True)
class BuildMetadata:
    version: str
    release_channel: ReleaseChannel
    commit: str | None
    built_at: str | None

    def as_dict(self) -> dict[str, str | None]:
        return asdict(self)


def release_channel(version: str = VERSION, environment: str | None = None) -> ReleaseChannel:
    requested = environment if environment is not None else os.environ.get("LAB_RELEASE_CHANNEL")
    if requested is not None:
        normalized = requested.strip().casefold()
        if normalized not in {"stable", "preview", "nightly"}:
            raise ValueError("LAB_RELEASE_CHANNEL must be stable, preview, or nightly")
        return cast(ReleaseChannel, normalized)
    lowered = version.casefold()
    if "dev" in lowered or "nightly" in lowered:
        return "nightly"
    if any(marker in lowered for marker in ("alpha", "beta", "-rc", "a0", "b0", "rc")):
        return "preview"
    return "stable"


def build_metadata(environ: dict[str, str] | None = None) -> BuildMetadata:
    source = os.environ if environ is None else environ
    commit = source.get("LAB_BUILD_COMMIT")
    if commit is not None:
        commit = commit.strip()
        commit = None if not _COMMIT_PATTERN.fullmatch(commit) else commit.casefold()
    built_at = source.get("LAB_BUILD_DATE")
    if built_at is not None:
        built_at = built_at.strip()
        if not _BUILD_DATE_PATTERN.fullmatch(built_at):
            built_at = None
    return BuildMetadata(
        version=VERSION,
        release_channel=release_channel(VERSION, source.get("LAB_RELEASE_CHANNEL")),
        commit=commit,
        built_at=built_at,
    )


__all__ = [
    "API_VERSION",
    "PLUGIN_API_VERSION",
    "BuildMetadata",
    "ReleaseChannel",
    "build_metadata",
    "release_channel",
]
