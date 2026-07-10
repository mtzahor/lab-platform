from __future__ import annotations

import argparse
import asyncio
from pathlib import Path

from lab_platform.agent.runtime import LabAgent, create_agent
from lab_platform.agent.server import AgentHttpServer
from lab_platform.core import VERSION


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    agent = create_agent(args.config_dir)
    asyncio.run(agent.start())
    _print_startup(agent)

    if args.once:
        asyncio.run(agent.shutdown())
        return 0

    host = args.host or agent.config.agent.host
    port = args.port or agent.config.agent.port
    server = AgentHttpServer(agent=agent, host=host, port=port)
    try:
        print(f"Listening on http://{server.host}:{server.port}")
        server.serve_forever()
    except KeyboardInterrupt:
        print("Shutting down Lab Agent")
    finally:
        server.shutdown()
        asyncio.run(agent.shutdown())
    return 0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="lab-agent")
    parser.add_argument(
        "--config-dir",
        type=Path,
        default=Path("config"),
        help="Directory containing agent.yaml and simlab.yaml.",
    )
    parser.add_argument("--host", default=None, help="Override the configured HTTP host.")
    parser.add_argument("--port", type=int, default=None, help="Override the configured HTTP port.")
    parser.add_argument(
        "--once",
        action="store_true",
        help="Start the agent, print the startup summary, and exit without serving HTTP.",
    )
    return parser


def _print_startup(agent: LabAgent) -> None:
    print(f"Lab Agent v{VERSION}")
    print("✓ Configuration loaded")
    print("✓ Logging initialized")
    print("✓ Event bus started")
    if agent.config.simlab.enabled:
        print("✓ SimLab started")
    else:
        print("✓ SimLab disabled")
    print(f"✓ {len(agent.benches())} benches registered")
    print(f"✓ {len(agent.plugins())} plugins loaded")
    print()


if __name__ == "__main__":
    raise SystemExit(main())
