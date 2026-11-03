from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from lab_platform.config import (
    CONFIG_VERSION,
    AgentIdentitySettings,
    AgentReconnectSettings,
    PlatformConfig,
    RealBackendSettings,
    SimLabBackendSettings,
    load_config,
    validate_config,
)
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

from tests.support.paths import REPOSITORY_ROOT


def test_missing_files_use_defaults(tmp_path: Path) -> None:
    config = load_config(tmp_path)

    assert config.agent.name == "local-agent"
    assert config.agent.port == 8080
    assert config.agent.max_request_body_size_mb == 1
    assert config.agent.data_directory == Path(".lab-agent")
    assert not config.control_plane.enabled
    assert config.identity.agent_id is None
    assert config.simlab.benches == 5
    assert config.plugins == ["power", "serial", "flash"]
    assert config.ci.default_reservation_minutes == 30
    assert config.ci.heartbeat_timeout_seconds == 120
    assert config.artifacts.max_upload_size_mb == 100
    assert config.serial.stream_buffer_lines == 500


def test_distributed_agent_configuration_requires_stable_identity_and_url(
    tmp_path: Path,
) -> None:
    (tmp_path / "agent.yaml").write_text(
        "control_plane:\n  enabled: true\n",
        encoding="utf-8",
    )
    with pytest.raises(ValidationError, match="control_plane.url"):
        load_config(tmp_path)

    (tmp_path / "agent.yaml").write_text(
        """
agent:
  name: home-lab
  data_directory: ./.agent-state
  location: jerusalem-home
  labels:
    environment: development
control_plane:
  enabled: true
  url: wss://lab.example.internal/api/v1/agent-gateway
  reconnect:
    initial_delay_seconds: 2
    maximum_delay_seconds: 20
    jitter: false
identity:
  agent_id: 11111111-1111-4111-8111-111111111111
  credential_env_var: HOME_LAB_AGENT_CREDENTIAL
""".strip(),
        encoding="utf-8",
    )

    config = load_config(tmp_path)

    assert config.agent.location == "jerusalem-home"
    assert config.agent.labels == {"environment": "development"}
    assert config.control_plane.enabled
    assert config.control_plane.reconnect.maximum_delay_seconds == 20
    assert config.identity.agent_id is not None
    assert config.identity.credential_env_var == "HOME_LAB_AGENT_CREDENTIAL"


def test_distributed_agent_reconnect_and_credential_settings_are_bounded() -> None:
    with pytest.raises(ValidationError, match="maximum reconnect delay"):
        AgentReconnectSettings(initial_delay_seconds=10, maximum_delay_seconds=1)

    with pytest.raises(ValidationError):
        AgentIdentitySettings(credential_env_var="not-valid-name")


@pytest.mark.parametrize(
    "contents",
    [
        "ci:\n  default_reservation_minutes: 121\n  maximum_reservation_minutes: 120\n",
        "ci:\n  heartbeat_interval_seconds: 120\n  heartbeat_timeout_seconds: 120\n",
        "ci:\n  session_timeout_seconds: 60\n  workflow_timeout_seconds: 61\n",
        "artifacts:\n  max_upload_size_mb: 0\n",
        "agent:\n  max_request_body_size_mb: 0\n",
        "serial:\n  decode_errors: unsafe\n",
    ],
)
def test_phase4_configuration_rejects_unsafe_limits(tmp_path: Path, contents: str) -> None:
    (tmp_path / "agent.yaml").write_text(contents, encoding="utf-8")

    with pytest.raises(ValidationError):
        load_config(tmp_path)


def test_files_are_deep_merged_and_validated(tmp_path: Path) -> None:
    (tmp_path / "agent.yaml").write_text(
        "agent:\n  name: test-agent\n  port: 9090\nplugins:\n  - power\n",
        encoding="utf-8",
    )
    (tmp_path / "simlab.yaml").write_text(
        "agent:\n  log_level: DEBUG\nsimlab:\n  benches: 2\n",
        encoding="utf-8",
    )

    config = validate_config(tmp_path)

    assert config.agent.name == "test-agent"
    assert config.agent.port == 9090
    assert config.agent.log_level == "DEBUG"
    assert config.simlab.benches == 2
    assert config.plugins == ["power"]


@pytest.mark.parametrize(
    "contents, exception",
    [
        ("- not-a-mapping\n", ValueError),
        ("agent: [unterminated\n", ValueError),
        ("agent:\n  unexpected: true\n", ValidationError),
        ("agent:\n  port: 70000\n", ValidationError),
    ],
)
def test_invalid_configuration_fails(
    tmp_path: Path,
    contents: str,
    exception: type[Exception],
) -> None:
    (tmp_path / "agent.yaml").write_text(contents, encoding="utf-8")

    with pytest.raises(exception):
        load_config(tmp_path)


def test_empty_yaml_file_uses_defaults(tmp_path: Path) -> None:
    (tmp_path / "agent.yaml").write_text("", encoding="utf-8")

    assert load_config(tmp_path).agent.name == "local-agent"


def test_phase2_configuration_is_exposed_as_one_effective_backend(tmp_path: Path) -> None:
    (tmp_path / "agent.yaml").write_text(
        "backend:\n  type: simlab\nsimlab:\n  benches: 7\n  speed_multiplier: 50\n",
        encoding="utf-8",
    )

    config = load_config(tmp_path)

    assert config.backends == []
    assert len(config.effective_backends) == 1
    backend = config.effective_backends[0]
    assert isinstance(backend, SimLabBackendSettings)
    assert backend.id == "simlab"
    assert backend.config.bench_count == 7
    assert backend.config.speed_multiplier == 50


def test_simlab_labels_flow_to_effective_backend(tmp_path: Path) -> None:
    (tmp_path / "simlab.yaml").write_text(
        "simlab:\n  labels:\n    board: esp32\n    location: simulation\n",
        encoding="utf-8",
    )

    backend = load_config(tmp_path).effective_backends[0]

    assert isinstance(backend, SimLabBackendSettings)
    assert backend.config.labels == {"board": "esp32", "location": "simulation"}


def test_phase3_mixed_backends_are_loaded_from_backends_yaml(tmp_path: Path) -> None:
    (tmp_path / "backends.yaml").write_text(
        """backends:
  - id: virtual-lab
    type: simlab
    config:
      benches: 10
      bench_prefix: virtual
      clock_mode: accelerated
      speed_multiplier: 5
  - id: local-hardware
    type: real
    config:
      benches:
        - id: esp32-devkit-01
          name: ESP32 DevKit V1
          labels:
            board: esp32
            location: home-lab
reservations:
  default_duration_minutes: 20
  maximum_duration_minutes: 120
scheduler:
  poll_interval_seconds: 0.5
workflows:
  definitions_directory: ./examples/workflows
""",
        encoding="utf-8",
    )

    config = load_config(tmp_path)

    assert [backend.id for backend in config.backends] == ["virtual-lab", "local-hardware"]
    simulated = config.backends[0]
    physical = config.backends[1]
    assert isinstance(simulated, SimLabBackendSettings)
    assert simulated.config.bench_count == 10
    assert simulated.config.bench_prefix == "virtual"
    assert isinstance(physical, RealBackendSettings)
    assert physical.config.benches[0].labels == {
        "board": "esp32",
        "location": "home-lab",
    }
    assert config.reservations.default_duration_minutes == 20
    assert config.scheduler.poll_interval_seconds == 0.5
    assert config.workflows.definitions_directory == Path("examples/workflows")


def test_simlab_backend_loads_relative_external_config_with_inline_overrides(
    tmp_path: Path,
) -> None:
    (tmp_path / "simlab-external.yaml").write_text(
        "simlab:\n"
        "  bench_count: 8\n"
        "  bench_prefix: external\n"
        "  auto_start: false\n"
        "  speed_multiplier: 2\n",
        encoding="utf-8",
    )
    config_file = tmp_path / "agent.yaml"
    config_file.write_text(
        "backends:\n"
        "  - id: virtual-lab\n"
        "    type: simlab\n"
        "    config:\n"
        "      config_path: ./simlab-external.yaml\n"
        "      benches: 3\n"
        "      speed_multiplier: 5\n",
        encoding="utf-8",
    )

    config = load_config(config_file)

    backend = config.backends[0]
    assert isinstance(backend, SimLabBackendSettings)
    assert backend.config.config_path == tmp_path / "simlab-external.yaml"
    assert backend.config.bench_count == 3
    assert backend.config.bench_prefix == "external"
    assert backend.config.auto_start is False
    assert backend.config.speed_multiplier == 5


def test_simlab_backend_config_path_accepts_full_platform_config(tmp_path: Path) -> None:
    external = tmp_path / "external-platform.yaml"
    external.write_text(
        "backends:\n"
        "  - id: selected\n"
        "    type: simlab\n"
        "    config:\n"
        "      benches: 12\n"
        "      bench_prefix: selected\n"
        "  - id: fallback\n"
        "    type: simlab\n"
        "    config:\n"
        "      benches: 99\n",
        encoding="utf-8",
    )
    config_file = tmp_path / "agent.yaml"
    config_file.write_text(
        "backends:\n"
        "  - id: selected\n"
        "    type: simlab\n"
        "    config:\n"
        "      config_path: ./external-platform.yaml\n"
        "      bench_count: 4\n",
        encoding="utf-8",
    )

    config = load_config(config_file)

    backend = config.backends[0]
    assert isinstance(backend, SimLabBackendSettings)
    assert backend.config.bench_count == 4
    assert backend.config.bench_prefix == "selected"


def test_simlab_backend_config_path_must_exist(tmp_path: Path) -> None:
    (tmp_path / "agent.yaml").write_text(
        "backends:\n"
        "  - id: virtual-lab\n"
        "    type: simlab\n"
        "    config:\n"
        "      config_path: ./missing.yaml\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="config_path does not exist"):
        load_config(tmp_path)


@pytest.mark.parametrize(
    "backends_yaml",
    [
        """backends:
  - id: duplicate
    type: simlab
  - id: duplicate
    type: simlab
""",
        """backends:
  - id: hardware-a
    type: real
    config:
      benches:
        - id: shared-bench
          name: First
  - id: hardware-b
    type: real
    config:
      benches:
        - id: shared-bench
          name: Second
""",
    ],
)
def test_phase3_configuration_rejects_duplicate_ids(
    tmp_path: Path,
    backends_yaml: str,
) -> None:
    (tmp_path / "backends.yaml").write_text(backends_yaml, encoding="utf-8")

    with pytest.raises(ValidationError):
        load_config(tmp_path)


def test_reservation_default_duration_cannot_exceed_maximum(tmp_path: Path) -> None:
    (tmp_path / "agent.yaml").write_text(
        "reservations:\n  default_duration_minutes: 60\n  maximum_duration_minutes: 30\n",
        encoding="utf-8",
    )

    with pytest.raises(ValidationError):
        load_config(tmp_path)


@pytest.mark.parametrize(
    "path",
    [Path("examples/phase3-team-agent.yaml"), Path("examples/simlab-team.yaml")],
)
def test_phase3_example_configuration_is_valid(path: Path) -> None:
    config = load_config(path)

    assert config.backends
    assert config.effective_backends == tuple(config.backends)


ROOT = REPOSITORY_ROOT


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
