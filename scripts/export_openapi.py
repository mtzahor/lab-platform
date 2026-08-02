from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from lab_platform.agent import create_agent
from lab_platform.agent import create_app as create_agent_app
from lab_platform.control_plane import (
    ControlPlaneConfig,
    ControlPlaneRuntime,
)
from lab_platform.control_plane import (
    create_app as create_control_plane_app,
)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Export or verify a checked-in Lab Platform OpenAPI contract."
    )
    parser.add_argument(
        "--service",
        choices=("agent", "control-plane"),
        default="agent",
        help="API application to describe (default: %(default)s).",
    )
    parser.add_argument("--check", action="store_true")
    parser.add_argument("output", type=Path, nargs="?")
    args = parser.parse_args()

    schema = _schema(args.service)
    rendered = json.dumps(schema, indent=2, sort_keys=True) + "\n"
    if args.check:
        if args.output is None:
            parser.error("--check requires an output file")
        if (
            not args.output.is_file()
            or json.loads(args.output.read_text(encoding="utf-8")) != schema
        ):
            print(f"OpenAPI schema is out of date: {args.output}")
            return 1
        print(f"OpenAPI schema is current: {args.output}")
        return 0
    if args.output is None:
        print(rendered, end="")
    else:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered, encoding="utf-8")
        print(f"Wrote OpenAPI schema: {args.output}")
    return 0


def _schema(service: str) -> dict[str, Any]:
    if service == "agent":
        return create_agent_app(create_agent("config")).openapi()
    runtime = ControlPlaneRuntime(
        ControlPlaneConfig.model_validate(
            {
                "control_plane": {"public_url": "http://127.0.0.1:8443"},
                "development": {"allow_insecure_agent_transport": True},
            }
        )
    )
    return create_control_plane_app(runtime).openapi()


if __name__ == "__main__":
    raise SystemExit(main())
