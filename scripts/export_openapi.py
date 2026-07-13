from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from lab_platform.agent import create_agent, create_app


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    parser.add_argument("output", type=Path, nargs="?")
    args = parser.parse_args()

    schema: dict[str, Any] = create_app(create_agent("config")).openapi()
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


if __name__ == "__main__":
    raise SystemExit(main())
