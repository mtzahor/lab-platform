from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path

from lab_platform.plugin_sdk.scaffold import create_plugin_scaffold


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="lab-plugin")
    commands = parser.add_subparsers(dest="command", required=True)
    initialize = commands.add_parser("init", help="Create a Plugin API 1.0 project.")
    initialize.add_argument("name")
    initialize.add_argument("--directory", type=Path, default=Path.cwd())
    args = parser.parse_args(argv)
    try:
        path = create_plugin_scaffold(args.name, args.directory)
    except (OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(f"Created plugin project: {path}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
