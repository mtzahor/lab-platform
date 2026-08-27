from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from lab_platform.config import CONFIG_VERSION, PlatformConfig, load_config
from lab_platform.control_plane.config import ControlPlaneConfig, load_control_plane_config
from lab_platform.core import (
    WorkflowSchemaVersionUnsupportedError,
    parse_workflow_yaml,
)
from lab_platform.models import (
    WORKFLOW_API_VERSION,
    WORKFLOW_KIND,
    WorkflowDefinition,
    workflow_document_payload,
)
from pydantic import ValidationError

ROOT = Path(__file__).resolve().parents[2]


def _workflow_document(*, api_version: str = WORKFLOW_API_VERSION) -> dict[str, object]:
    return {
        "apiVersion": api_version,
        "kind": WORKFLOW_KIND,
        "metadata": {
            "name": "versioned-smoke",
            "version": 3,
            "description": "Exercise the stable workflow document envelope.",
        },
        "spec": {
            "requirements": {"capabilities": ["probe"]},
            "steps": [{"action": "probe"}],
        },
    }


def test_workflow_v1_document_parses_and_round_trips() -> None:
    document = _workflow_document()
    definition = WorkflowDefinition.model_validate(document)

    assert definition.name == "versioned-smoke"
    assert definition.version == 3
    assert definition.requirements.capabilities == ["probe"]
    assert workflow_document_payload(definition) == {
        **document,
        "spec": {
            "inputs": {},
            "requirements": {"capabilities": ["probe"], "labels": {}},
            "steps": [{"name": None, "action": "probe"}],
        },
    }


def test_workflow_parser_rejects_an_unsupported_api_version_clearly() -> None:
    source = yaml.safe_dump(_workflow_document(api_version="lab.platform/v2"), sort_keys=False)

    with pytest.raises(WorkflowSchemaVersionUnsupportedError) as captured:
        parse_workflow_yaml(source, source_name="future.yaml")

    assert captured.value.code == "WORKFLOW_SCHEMA_VERSION_UNSUPPORTED"
    assert "lab.platform/v2" in captured.value.message
    assert captured.value.details == {
        "source": "future.yaml",
        "received_api_version": "lab.platform/v2",
        "supported_api_versions": [WORKFLOW_API_VERSION],
    }


def test_legacy_flat_workflow_remains_readable_during_v1_deprecation_window() -> None:
    definition = parse_workflow_yaml(
        """
name: legacy-smoke
version: 1
requirements: {capabilities: [probe]}
steps:
  - action: probe
"""
    )

    assert definition.name == "legacy-smoke"
    assert workflow_document_payload(definition)["apiVersion"] == WORKFLOW_API_VERSION


def test_agent_and_control_plane_configuration_versions_are_strict() -> None:
    assert PlatformConfig().config_version == CONFIG_VERSION
    assert (
        ControlPlaneConfig.model_validate(
            {
                "config_version": 1,
                "control_plane": {"public_url": "http://127.0.0.1:8443"},
                "development": {"allow_insecure_agent_transport": True},
            }
        ).config_version
        == CONFIG_VERSION
    )

    with pytest.raises(ValidationError, match="config_version"):
        PlatformConfig.model_validate({"config_version": 2})
    with pytest.raises(ValidationError, match="config_version"):
        ControlPlaneConfig.model_validate(
            {
                "config_version": 2,
                "control_plane": {"public_url": "http://127.0.0.1:8443"},
                "development": {"allow_insecure_agent_transport": True},
            }
        )


def test_checked_in_configs_and_workflows_declare_their_public_versions() -> None:
    agent_configs = [
        ROOT / "config/agent.yaml",
        ROOT / "config/simlab.yaml",
        ROOT / "examples/esp32-local.yaml",
        ROOT / "examples/phase3-team-agent.yaml",
        ROOT / "examples/simlab-team.yaml",
    ]
    control_plane_configs = [
        ROOT / "config/control-plane.yaml",
        ROOT / "config/control-plane.postgresql.yaml",
        ROOT / "deploy/acceptance/control-plane.yaml",
        ROOT / "deploy/demo/control-plane.yaml",
        ROOT / "deploy/production/control-plane.yaml",
        ROOT / "apps/cli/src/lab_platform/cli/deployment/demo/control-plane.yaml",
        ROOT / "apps/cli/src/lab_platform/cli/deployment/production/control-plane.yaml",
    ]

    for path in agent_configs:
        assert yaml.safe_load(path.read_text(encoding="utf-8"))["config_version"] == CONFIG_VERSION
    for path in control_plane_configs:
        assert yaml.safe_load(path.read_text(encoding="utf-8"))["config_version"] == CONFIG_VERSION

    assert load_config(ROOT / "config").config_version == CONFIG_VERSION
    assert load_control_plane_config(ROOT / "config/control-plane.yaml").config_version == 1
    for path in sorted((ROOT / "examples/workflows").glob("*.yaml")):
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        assert raw["apiVersion"] == WORKFLOW_API_VERSION
        assert raw["kind"] == WORKFLOW_KIND
        assert parse_workflow_yaml(path.read_text(encoding="utf-8"), source_name=str(path))
