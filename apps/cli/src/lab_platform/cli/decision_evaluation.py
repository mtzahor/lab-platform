from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
from typing import Any

from lab_platform.core.decision_engine.evaluation import evaluate
from lab_platform.core.decision_engine.settings import DecisionSettings


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Evaluate diagnostic fixtures; live requests require explicit --live."
    )
    parser.add_argument("--fixtures", type=Path, required=True)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument(
        "--responses", type=Path, help="Offline recorded provider responses keyed by run ID"
    )
    group.add_argument("--live", action="store_true")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    # Provider-specific parsing stays in the adapter, including offline replay.
    from lab_platform.control_plane.jev_provider import JevDecisionEngine

    # Offline replay is deliberately independent of deployment flags and keys.
    # Live evaluation opts into the process environment explicitly.
    settings = (
        DecisionSettings(enabled=True)
        if args.responses is not None
        else DecisionSettings(enabled=True).with_environment()
    )
    if args.live and (not settings.enabled or not settings.api_key.get_secret_value()):
        parser.error("Live evaluation requires JEV_ENABLED=true and JEV_API_KEY")
    responses = json.loads(args.responses.read_text()) if args.responses else None

    async def replay(state: str, questions: dict[str, Any]) -> dict[str, Any]:
        del questions
        assert responses is not None
        return dict(responses[json.loads(state)["run_id"]])

    engine = JevDecisionEngine(settings, request=replay if responses is not None else None)
    report = asyncio.run(evaluate(json.loads(args.fixtures.read_text()), engine, settings))
    rendered = json.dumps(report, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.write_text(rendered)
    else:
        print(rendered, end="")
    return int(bool(report["regressions"]))


if __name__ == "__main__":
    raise SystemExit(main())
