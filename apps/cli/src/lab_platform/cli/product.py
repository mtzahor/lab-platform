from __future__ import annotations

import argparse
import os
import secrets
import stat
import subprocess
from importlib import resources
from importlib.resources.abc import Traversable
from pathlib import Path
from urllib.parse import quote

from lab_platform.core import VERSION


def main(argv: list[str] | None = None) -> int:
    """Initialize and operate the supported local deployment layouts."""

    parser = _build_parser()
    args = parser.parse_args(argv)
    if args.command == "init":
        return _initialize(Path(args.directory), mode="demo" if args.demo else "production")
    if args.command == "dev":
        return _run_development(args)
    parser.error("a command is required")


def _initialize(destination: Path, *, mode: str) -> int:
    destination = destination.expanduser().resolve()
    template = _deployment_template(mode)
    files = _template_files(template)
    if not files:
        raise RuntimeError(f"The {mode} deployment template is missing from this installation.")

    generated = {Path(".env")}
    if mode == "production":
        generated.update(
            {
                Path("secrets/control-plane-secret-key"),
                Path("secrets/database-url"),
                Path("secrets/postgres-password"),
            }
        )
    collisions = sorted(
        path for path in (*files.keys(), *generated) if (destination / path).exists()
    )
    if collisions:
        rendered = ", ".join(str(path) for path in collisions[:5])
        if len(collisions) > 5:
            rendered += f", and {len(collisions) - 5} more"
        raise FileExistsError(
            f"Refusing to overwrite an existing deployment in {destination}: {rendered}"
        )

    destination.mkdir(parents=True, exist_ok=True)
    for relative, content in files.items():
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)

    example = destination / ".env.example"
    env_file = destination / ".env"
    env_file.write_bytes(example.read_bytes() if example.exists() else b"")
    if mode == "production":
        secret_directory = destination / "secrets"
        secret_directory.mkdir(mode=0o700)
        os.chmod(secret_directory, 0o700)
        database_password = secrets.token_urlsafe(32)
        _write_secret(secret_directory / "postgres-password", database_password)
        encoded_password = quote(database_password, safe="")
        _write_secret(
            secret_directory / "database-url",
            f"postgresql://lab_platform:{encoded_password}@postgres:5432/lab_platform",
        )
        _write_secret(secret_directory / "control-plane-secret-key", secrets.token_hex(32))

    compose_file = _compose_file(destination)
    print(f"Lab Platform {VERSION}")
    print(f"Initialized {mode} deployment: {destination}")
    if mode == "production":
        print("Secrets were generated with owner-only permissions.")
    print(f"Next: docker compose -f {compose_file} up -d")
    return 0


def _run_development(args: argparse.Namespace) -> int:
    destination = Path(args.directory).expanduser().resolve()
    if not destination.exists():
        _initialize(destination, mode="demo")
    compose = _compose_file(destination)
    command = ["docker", "compose", "-f", str(compose), args.action]
    if args.action == "up" and args.detach:
        command.append("--detach")
    if args.action == "logs" and args.follow:
        command.append("--follow")
    return _execute(command, cwd=destination)


def _deployment_template(mode: str) -> Traversable:
    return resources.files("lab_platform.cli").joinpath("deployment", mode)


def _template_files(root: Traversable) -> dict[Path, bytes]:
    files: dict[Path, bytes] = {}

    def walk(entry: Traversable, relative: Path) -> None:
        if entry.is_dir():
            for child in entry.iterdir():
                walk(child, relative / child.name)
            return
        files[relative] = entry.read_bytes()

    if root.is_dir():
        for child in root.iterdir():
            walk(child, Path(child.name))
    return files


def _execute(command: list[str], *, cwd: Path) -> int:
    try:
        completed = subprocess.run(command, cwd=cwd, check=False)
    except FileNotFoundError as exc:
        raise RuntimeError(
            "Docker was not found. Install Docker with the Compose plugin and try again."
        ) from exc
    return completed.returncode


def _write_secret(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, stat.S_IRUSR | stat.S_IWUSR)
    try:
        os.write(descriptor, f"{value}\n".encode())
    finally:
        os.close(descriptor)


def _compose_file(directory: Path) -> Path:
    for name in ("compose.yaml", "docker-compose.yml", "docker-compose.yaml"):
        candidate = directory / name
        if candidate.is_file():
            return candidate
    raise FileNotFoundError(f"No Compose file exists in {directory}")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="lab-platform", allow_abbrev=False)
    subcommands = parser.add_subparsers(dest="command", required=True)

    initialize = subcommands.add_parser(
        "init", help="Generate a self-hosted deployment without overwriting existing files."
    )
    initialize.add_argument("directory", nargs="?", default="lab-platform-deployment")
    initialize.add_argument(
        "--demo",
        action="store_true",
        help="Generate the clearly marked, non-production demo deployment.",
    )

    development = subcommands.add_parser("dev", help="Operate the packaged demo deployment.")
    development.add_argument("action", choices=("up", "down", "logs", "ps"))
    development.add_argument(
        "--directory", default=".lab-platform-demo", help="Demo deployment directory."
    )
    development.add_argument("--detach", action="store_true", help="Run `dev up` detached.")
    development.add_argument("--follow", action="store_true", help="Follow `dev logs`.")
    return parser


if __name__ == "__main__":
    raise SystemExit(main())
