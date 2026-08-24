#!/usr/bin/env python3
"""Create the disposable organisation, Agent, and workflow for demo Compose."""

from __future__ import annotations

import asyncio
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import yaml
from lab_platform.agent_protocol import PROTOCOL_VERSION
from lab_platform.control_plane.config import load_control_plane_config
from lab_platform.control_plane.runtime import ControlPlaneRuntime
from lab_platform.core import VERSION
from lab_platform.models import WorkflowDefinition


async def bootstrap() -> None:
    config_path = Path(
        os.environ.get(
            "LAB_DEMO_CONTROL_PLANE_CONFIG",
            "/etc/lab-platform/control-plane.yaml",
        )
    )
    output = Path(os.environ.get("LAB_DEMO_STATE_DIR", "/run/lab-platform-demo"))
    output.mkdir(parents=True, exist_ok=True)
    config = load_control_plane_config(config_path)
    runtime = ControlPlaneRuntime(config)
    runtime.database.initialize()
    try:
        default_organisation = await runtime.identity_repository.ensure_default_organisation(
            slug=config.identity.default_organisation_slug,
            name=config.identity.default_organisation_name,
        )
        organisation, _owner = await runtime.identity_administration.bootstrap_admin(
            organisation_slug=default_organisation.slug,
            organisation_name=default_organisation.name,
            username="demo-admin",
            display_name="Demo Administrator",
            password="LabPlatform-Demo-Only!",
            recovery=True,
        )
        for agent in await runtime.enrollment.list_agents(
            organisation_id=organisation.id,
            allow_internal_authorisation=True,
        ):
            if agent.name == "demo-simlab" and agent.revoked_at is None:
                await runtime.enrollment.revoke_agent(
                    agent.id,
                    organisation_id=organisation.id,
                    allow_internal_authorisation=True,
                )
        issued = await runtime.enrollment.issue_token(
            name="demo-simlab",
            expires_at=datetime.now(UTC) + timedelta(minutes=10),
            organisation_id=organisation.id,
            allowed_labels={"deployment": "disposable-demo"},
            allow_internal_authorisation=True,
        )
        enrolled = await runtime.enrollment.enroll(
            plaintext_token=issued.plaintext.get_secret_value(),
            request_id=uuid4(),
            agent_version=VERSION,
            protocol_version=PROTOCOL_VERSION,
            location="demo-compose",
        )
        await runtime.workflow_repository.save_definition(
            WorkflowDefinition.model_validate(
                {
                    "organisation_id": organisation.id,
                    "name": "demo-smoke-test",
                    "version": 1,
                    "description": "Disposable SimLab smoke test from the Phase 8 demo.",
                    "requirements": {"capabilities": ["probe", "serial"]},
                    "steps": [
                        {"name": "Probe virtual target", "action": "probe"},
                        {
                            "name": "Read startup serial",
                            "action": "read_serial",
                            "until_pattern": "^READY$",
                            "timeout_seconds": 5,
                            "max_lines": 100,
                        },
                        {
                            "name": "Assert ready marker",
                            "action": "assert_serial",
                            "pattern": "^READY$",
                        },
                    ],
                }
            )
        )
        _write_agent_config(output / "agent.yaml", str(enrolled.agent.id))
        credential = output / "agent-credential"
        credential.write_text(enrolled.plaintext.get_secret_value() + "\n", encoding="utf-8")
        credential.chmod(0o600)
        print("Demo bootstrap complete. This deployment is NOT FOR PRODUCTION.")
        print("Login: demo-admin / LabPlatform-Demo-Only!")
    finally:
        runtime.database.close()


def _write_agent_config(path: Path, agent_id: str) -> None:
    payload = {
        "agent": {
            "name": "demo-simlab",
            "host": "127.0.0.1",
            "port": 8081,
            "log_level": "INFO",
            "data_directory": "/var/lib/lab-platform/data",
            "location": "demo-compose",
            "labels": {"deployment": "disposable-demo"},
        },
        "control_plane": {
            "enabled": True,
            "url": "ws://127.0.0.1:8443/api/v1/agent-gateway",
            "allow_insecure_loopback": True,
        },
        "identity": {
            "agent_id": agent_id,
            "credential_env_var": "LAB_DEMO_AGENT_CREDENTIAL",
        },
        "backend": {"type": "simlab"},
        "simlab": {
            "enabled": True,
            "benches": 2,
            "bench_prefix": "virtual-esp32",
            "auto_start": True,
            "clock_mode": "accelerated",
            "speed_multiplier": 100,
            "flash_duration_seconds": 0,
            "labels": {"board": "esp32", "deployment": "disposable-demo"},
        },
        "database": {"url": "sqlite:////var/lib/lab-platform/data/agent.db"},
        "artifacts": {
            "directory": "/var/lib/lab-platform/artifacts",
            "max_firmware_size_mb": 100,
            "max_upload_size_mb": 100,
        },
        "operations": {"poll_interval_ms": 25, "shutdown_timeout_seconds": 10},
        "scheduler": {"poll_interval_seconds": 0.1, "automatic_assignment": True},
        "workflows": {
            "definitions_directory": "/var/lib/lab-platform/workflows",
            "definition_paths": [],
        },
        "plugins": ["power", "serial", "firmware"],
    }
    path.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")
    path.chmod(0o644)


if __name__ == "__main__":
    asyncio.run(bootstrap())
