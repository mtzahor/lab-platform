from __future__ import annotations

import html
import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True, slots=True)
class CiSummaryStep:
    name: str
    status: str


@dataclass(frozen=True, slots=True)
class HardwareCiSummary:
    status: str
    bench_id: str | None = None
    backend: str | None = None
    firmware: str | None = None
    duration_seconds: float | None = None
    steps: Sequence[CiSummaryStep] = field(default_factory=tuple)
    artifacts: Sequence[str] = field(default_factory=tuple)
    cleanup_status: str | None = None


def render_github_summary(summary: HardwareCiSummary) -> str:
    """Render a GitHub-flavored Markdown job summary from provider-neutral data."""

    fields = [
        ("Status", summary.status),
        ("Bench", summary.bench_id),
        ("Backend", summary.backend),
        ("Firmware", summary.firmware),
        (
            "Duration",
            _duration(summary.duration_seconds) if summary.duration_seconds is not None else None,
        ),
        ("Cleanup", summary.cleanup_status),
    ]
    lines = ["## Hardware CI Result", "", "| Field | Value |", "| --- | --- |"]
    lines.extend(
        f"| {_inline(name)} | {_inline(value)} |" for name, value in fields if value is not None
    )

    if summary.steps:
        lines.extend(("", "### Steps", ""))
        lines.extend(
            f"- {_status_icon(step.status)} {_inline(step.name)} — {_inline(step.status)}"
            for step in summary.steps
        )
    if summary.artifacts:
        lines.extend(("", "### Artifacts", ""))
        lines.extend(f"- `{_code(artifact)}`" for artifact in summary.artifacts)
    return "\n".join(lines) + "\n"


def append_github_summary(
    summary: HardwareCiSummary,
    environment: Mapping[str, str] | None = None,
) -> Path | None:
    """Append to ``GITHUB_STEP_SUMMARY`` when running under GitHub Actions."""

    values: Mapping[str, str] = os.environ if environment is None else environment
    destination = values.get("GITHUB_STEP_SUMMARY")
    if destination is None or not destination.strip():
        return None
    path = Path(destination)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8", newline="\n") as stream:
        stream.write(render_github_summary(summary))
    return path


def _duration(seconds: float) -> str:
    if seconds < 60:
        return f"{seconds:g} seconds"
    minutes, remainder = divmod(int(seconds), 60)
    return f"{minutes}m {remainder}s"


def _inline(value: object) -> str:
    collapsed = " ".join(str(value).splitlines()).strip()
    return html.escape(collapsed, quote=False).replace("|", r"\|")


def _code(value: str) -> str:
    return html.escape(" ".join(value.splitlines()).strip(), quote=False).replace("`", "\\`")


def _status_icon(status: str) -> str:
    normalized = status.casefold()
    if normalized in {"succeeded", "success", "passed", "completed"}:
        return "✓"
    if normalized in {"failed", "failure", "error"}:
        return "✗"
    if normalized in {"skipped", "cancelled", "canceled"}:
        return "○"
    return "…"
