#!/usr/bin/env python3
"""Run an isolated control plane and connected SimLab Agent for browser tests."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import signal
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from contextlib import ExitStack
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TextIO
from uuid import uuid4

import yaml
from lab_platform.control_plane import ControlPlaneRuntime
from lab_platform.control_plane.config import load_control_plane_config
from lab_platform.core import VERSION
from lab_platform.models import WorkflowDefinition

_HOST = "127.0.0.1"
_ORGANISATION_SLUG = "simlab-demo"
_USERNAME = "michael"
_PASSWORD = "Phase7-browser-demo!"
_AGENT_CREDENTIAL_ENV = "LAB_PLATFORM_PHASE7_E2E_AGENT_CREDENTIAL"
_REPOSITORY_ROOT = Path(__file__).resolve().parents[1]


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--control-plane-port", type=int, default=18_127)
    parser.add_argument("--agent-port", type=int, default=18_128)
    parser.add_argument("--startup-timeout", type=float, default=45.0)
    return parser.parse_args()


def _write_yaml(path: Path, payload: object) -> None:
    path.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")


def _control_plane_config(root: Path, port: int) -> Path:
    base_url = f"http://{_HOST}:{port}"
    path = root / "control-plane.yaml"
    _write_yaml(
        path,
        {
            "control_plane": {
                "host": _HOST,
                "port": port,
                "public_url": base_url,
                "log_level": "WARNING",
            },
            "database": {"url": f"sqlite:///{root / 'control-plane.db'}"},
            "agent_gateway": {
                "heartbeat_interval_seconds": 1,
                "heartbeat_timeout_seconds": 5,
                "offline_timeout_seconds": 10,
                "handshake_timeout_seconds": 5,
                "monitor_interval_seconds": 0.1,
            },
            "artifacts": {
                "directory": str(root / "control-plane-artifacts"),
                "maximum_upload_size_mb": 100,
                "transfer_token_ttl_seconds": 60,
                "finalization_timeout_seconds": 30,
            },
            "identity": {
                "enabled": True,
                "default_organisation_slug": _ORGANISATION_SLUG,
                "default_organisation_name": "SimLab Demo",
                "local_auth": {"enabled": True, "minimum_password_length": 12},
                "oidc": {"enabled": False},
            },
            "audit": {"enabled": True, "retention_days": 30},
            "web": {
                "enabled": True,
                "public_url": base_url,
                "api_base_url": "/api/v1",
                "live_updates": {"sse_enabled": True, "polling_fallback_seconds": 2},
                "uploads": {"maximum_firmware_size_mb": 100},
            },
            "development": {
                "enabled": True,
                "allow_insecure_agent_transport": True,
            },
        },
    )
    return path


async def _seed_control_plane(config_path: Path) -> list[tuple[str, str]]:
    runtime = ControlPlaneRuntime(load_control_plane_config(config_path))
    runtime.database.initialize()
    try:
        await runtime.identity_repository.ensure_default_organisation(
            slug=_ORGANISATION_SLUG,
            name="SimLab Demo",
        )
        organisation, _owner = await runtime.identity_administration.bootstrap_admin(
            organisation_slug=_ORGANISATION_SLUG,
            organisation_name="SimLab Demo",
            username=_USERNAME,
            display_name="Michael Browser Operator",
            password=_PASSWORD,
        )
        enrolled_agents: list[tuple[str, str]] = []
        for suffix in ("alpha", "beta"):
            issued = await runtime.enrollment.issue_token(
                name=f"phase7-simlab-{suffix}",
                expires_at=datetime.now(UTC) + timedelta(minutes=5),
                organisation_id=organisation.id,
                allowed_labels={
                    "environment": "phase7-browser-e2e",
                    "cluster": suffix,
                },
                allow_internal_authorisation=True,
            )
            enrolled = await runtime.enrollment.enroll(
                plaintext_token=issued.plaintext.get_secret_value(),
                request_id=uuid4(),
                agent_version=VERSION,
                protocol_version="1.0",
                location=f"browser-integration-{suffix}",
            )
            enrolled_agents.append((str(enrolled.agent.id), enrolled.plaintext.get_secret_value()))
        await runtime.workflow_repository.save_definition(
            WorkflowDefinition.model_validate(
                {
                    "organisation_id": organisation.id,
                    "name": "phase7-cutline",
                    "version": 1,
                    "description": "A real control-plane to SimLab browser smoke workflow.",
                    "requirements": {"capabilities": ["probe", "serial"]},
                    "steps": [
                        {"name": "Probe the simulated target", "action": "probe"},
                        {
                            "name": "Capture startup serial",
                            "action": "read_serial",
                            "until_pattern": "^READY$",
                            "timeout_seconds": 5,
                            "max_lines": 100,
                        },
                        {
                            "name": "Confirm the simulated target is ready",
                            "action": "assert_serial",
                            "pattern": "^READY$",
                        },
                    ],
                }
            )
        )
        return enrolled_agents
    finally:
        runtime.database.close()


def _agent_config(
    root: Path,
    *,
    index: int,
    port: int,
    control_plane_port: int,
    agent_id: str,
) -> Path:
    suffix = "alpha" if index == 0 else "beta"
    path = root / f"agent-{suffix}.yaml"
    _write_yaml(
        path,
        {
            "agent": {
                "name": f"phase7-simlab-{suffix}",
                "host": _HOST,
                "port": port,
                "log_level": "WARNING",
                "data_directory": str(root / f"agent-{suffix}-data"),
                "location": f"browser-integration-{suffix}",
                "labels": {"environment": "phase7-browser-e2e", "cluster": suffix},
            },
            "control_plane": {
                "enabled": True,
                "url": f"ws://{_HOST}:{control_plane_port}/api/v1/agent-gateway",
                "heartbeat_interval_seconds": 1,
                "maximum_clock_skew_seconds": 30,
                "maximum_message_size_mb": 2,
                "outgoing_queue_size": 256,
                "event_batch_size": 100,
                "event_ack_timeout_seconds": 10,
                "allow_insecure_loopback": True,
                "reconnect": {
                    "initial_delay_seconds": 0.1,
                    "maximum_delay_seconds": 1.0,
                    "jitter": False,
                    "stability_seconds": 1.0,
                },
            },
            "identity": {
                "agent_id": agent_id,
                "credential_env_var": _AGENT_CREDENTIAL_ENV,
            },
            "backend": {"type": "simlab"},
            "simlab": {
                "enabled": True,
                "benches": 1,
                "bench_prefix": f"browser-{suffix}",
                "auto_start": True,
                "clock_mode": "accelerated",
                "speed_multiplier": 100,
                "flash_duration_seconds": 0,
                "labels": {
                    "board": "esp32",
                    "purpose": "phase7-browser-e2e",
                    "cluster": suffix,
                },
            },
            "database": {"url": f"sqlite:///{root / f'agent-{suffix}.db'}"},
            "artifacts": {
                "directory": str(root / f"agent-{suffix}-artifacts"),
                "max_firmware_size_mb": 100,
                "max_upload_size_mb": 100,
            },
            "operations": {"poll_interval_ms": 25, "shutdown_timeout_seconds": 5},
            "scheduler": {"poll_interval_seconds": 0.1, "automatic_assignment": True},
            "workflows": {
                "definitions_directory": str(root / f"agent-{suffix}-workflows"),
                "definition_paths": [],
            },
            "plugins": ["power", "serial", "firmware"],
        },
    )
    return path


def _start_process(
    command: list[str],
    *,
    log: TextIO,
    environment: dict[str, str] | None = None,
) -> subprocess.Popen[str]:
    return subprocess.Popen(
        command,
        cwd=_REPOSITORY_ROOT,
        env=environment,
        stdout=log,
        stderr=subprocess.STDOUT,
        text=True,
    )


def _wait_for_health(
    url: str,
    process: subprocess.Popen[str],
    *,
    timeout: float,
    log_path: Path,
) -> None:
    deadline = time.monotonic() + timeout
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(
                f"process exited with {process.returncode} before {url} was ready\n"
                f"{_tail(log_path)}"
            )
        try:
            with urllib.request.urlopen(url, timeout=1) as response:  # noqa: S310 - loopback only
                payload = json.loads(response.read())
                if response.status == 200 and payload.get("status") == "healthy":
                    return
        except (OSError, ValueError, urllib.error.URLError) as exc:
            last_error = exc
        time.sleep(0.1)
    raise RuntimeError(f"timed out waiting for {url}: {last_error}\n{_tail(log_path)}")


def _tail(path: Path, maximum_characters: int = 6_000) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")[-maximum_characters:]
    except OSError as exc:
        return f"<could not read {path}: {exc}>"


def _stop_process(process: subprocess.Popen[str] | None) -> None:
    if process is None or process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)


def main() -> int:
    args = _arguments()
    stop = threading.Event()
    for signal_name in (signal.SIGINT, signal.SIGTERM):
        signal.signal(signal_name, lambda _signum, _frame: stop.set())

    control_plane: subprocess.Popen[str] | None = None
    agents: list[subprocess.Popen[str]] = []
    with tempfile.TemporaryDirectory(prefix="lab-platform-phase7-e2e-") as temporary:
        root = Path(temporary)
        control_plane_config = _control_plane_config(root, args.control_plane_port)
        enrolled_agents = asyncio.run(_seed_control_plane(control_plane_config))
        agent_configs = [
            _agent_config(
                root,
                index=index,
                port=args.agent_port + index,
                control_plane_port=args.control_plane_port,
                agent_id=agent_id,
            )
            for index, (agent_id, _credential) in enumerate(enrolled_agents)
        ]
        for suffix in ("alpha", "beta"):
            (root / f"agent-{suffix}-workflows").mkdir()
        control_plane_log_path = root / "control-plane.log"
        agent_log_paths = [root / f"agent-{index + 1}.log" for index in range(len(agent_configs))]
        try:
            with ExitStack() as stack:
                control_plane_log = stack.enter_context(
                    control_plane_log_path.open("w", encoding="utf-8")
                )
                control_plane = _start_process(
                    [
                        sys.executable,
                        "-m",
                        "lab_platform.control_plane.cli",
                        "--config",
                        str(control_plane_config),
                    ],
                    log=control_plane_log,
                )
                _wait_for_health(
                    f"http://{_HOST}:{args.control_plane_port}/api/v1/health",
                    control_plane,
                    timeout=args.startup_timeout,
                    log_path=control_plane_log_path,
                )
                for index, ((_, credential), agent_config, agent_log_path) in enumerate(
                    zip(enrolled_agents, agent_configs, agent_log_paths, strict=True)
                ):
                    agent_environment = os.environ.copy()
                    agent_environment[_AGENT_CREDENTIAL_ENV] = credential
                    agent_log = stack.enter_context(agent_log_path.open("w", encoding="utf-8"))
                    agent_process = _start_process(
                        [
                            sys.executable,
                            "-m",
                            "lab_platform.agent.cli",
                            "--config",
                            str(agent_config),
                        ],
                        log=agent_log,
                        environment=agent_environment,
                    )
                    agents.append(agent_process)
                    _wait_for_health(
                        f"http://{_HOST}:{args.agent_port + index}/api/v1/health",
                        agent_process,
                        timeout=args.startup_timeout,
                        log_path=agent_log_path,
                    )
                print(
                    f"Phase 7 browser environment ready at "
                    f"http://{_HOST}:{args.control_plane_port}",
                    flush=True,
                )
                while not stop.wait(0.25):
                    processes = [("control plane", control_plane, control_plane_log_path)]
                    processes.extend(
                        (f"Agent {index + 1}", process, agent_log_paths[index])
                        for index, process in enumerate(agents)
                    )
                    for name, process, log_path in processes:
                        if process.poll() is not None:
                            raise RuntimeError(
                                f"{name} exited unexpectedly with {process.returncode}\n"
                                f"{_tail(log_path)}"
                            )
        except Exception as exc:
            print(f"Phase 7 browser environment failed: {exc}", file=sys.stderr, flush=True)
            return 1
        finally:
            for agent in reversed(agents):
                _stop_process(agent)
            _stop_process(control_plane)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
