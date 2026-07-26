from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest
from lab_platform.core.workflows import (
    WorkflowInvalidError,
    WorkflowService,
    parse_workflow_yaml,
    resolve_workflow_inputs,
)
from lab_platform.models import (
    ArtifactReference,
    ArtifactWorkflowInput,
    BenchSnapshot,
    BenchStatus,
    BooleanWorkflowInput,
    IntegerWorkflowInput,
    StringWorkflowInput,
    WorkflowAction,
    WorkflowDefinition,
    WorkflowStepResult,
    WorkflowStepStatus,
)

TYPED_WORKFLOW = """
name: esp32-ci-test
version: 2
inputs:
  firmware:
    type: artifact
    required: true
  expected_version:
    type: string
    required: true
  ready_timeout:
    type: integer
    default: 20
  feature_enabled:
    type: boolean
    default: true
requirements:
  capabilities: [firmware, serial]
  labels:
    board: esp32
steps:
  - name: Flash firmware
    action: flash
    firmware: "${{ inputs.firmware }}"
    version: "${{ inputs.expected_version }}"
  - name: Wait for boot
    action: read_serial
    until_pattern: "^READY=${{ inputs.feature_enabled }}$"
    timeout_seconds: "${{ inputs.ready_timeout }}"
  - name: Verify version
    action: assert_serial
    pattern: "^FIRMWARE_VERSION=${{ inputs.expected_version }}$"
"""


def test_typed_workflow_parsing_and_resolution(tmp_path: Path) -> None:
    definition = parse_workflow_yaml(TYPED_WORKFLOW, base_directory=tmp_path)
    artifact_id = uuid4()
    artifact_path = tmp_path / "objects" / artifact_id.hex
    seen: list[ArtifactReference] = []

    def resolve_artifact(reference: ArtifactReference) -> Path:
        seen.append(reference)
        return artifact_path

    resolved = resolve_workflow_inputs(
        definition,
        {
            "firmware": {"artifact_id": str(artifact_id)},
            "expected_version": "0.5.0",
        },
        artifact_resolver=resolve_artifact,
    )

    assert isinstance(definition.inputs["firmware"], ArtifactWorkflowInput)
    assert isinstance(definition.inputs["expected_version"], StringWorkflowInput)
    assert isinstance(definition.inputs["ready_timeout"], IntegerWorkflowInput)
    assert isinstance(definition.inputs["feature_enabled"], BooleanWorkflowInput)
    assert definition.requirements.labels == {"board": "esp32"}
    assert definition.steps[0].name == "Flash firmware"
    assert str(definition.steps[0].firmware) == "${{ inputs.firmware }}"  # type: ignore[union-attr]
    assert resolved.steps[0].firmware == artifact_path  # type: ignore[union-attr]
    assert resolved.steps[0].version == "0.5.0"  # type: ignore[union-attr]
    assert resolved.steps[1].timeout_seconds == 20  # type: ignore[union-attr]
    assert resolved.steps[1].until_pattern == "^READY=true$"  # type: ignore[union-attr]
    assert resolved.steps[2].pattern == "^FIRMWARE_VERSION=0.5.0$"  # type: ignore[union-attr]
    assert seen == [ArtifactReference(artifact_id=artifact_id)]


def test_declared_inputs_validate_types_defaults_and_unknown_names(tmp_path: Path) -> None:
    definition = parse_workflow_yaml(TYPED_WORKFLOW)
    artifact = {"artifact_id": str(uuid4())}

    def resolver(_reference: ArtifactReference) -> Path:
        return tmp_path / "firmware.bin"

    overridden = resolve_workflow_inputs(
        definition,
        {
            "firmware": artifact,
            "expected_version": "1.2.3",
            "ready_timeout": "30",
            "feature_enabled": "false",
        },
        artifact_resolver=resolver,
    )
    assert overridden.steps[1].timeout_seconds == 30  # type: ignore[union-attr]
    assert overridden.steps[1].until_pattern == "^READY=false$"  # type: ignore[union-attr]

    with pytest.raises(WorkflowInvalidError, match="expected_version.*required"):
        resolve_workflow_inputs(
            definition,
            {"firmware": artifact},
            artifact_resolver=resolver,
        )
    with pytest.raises(WorkflowInvalidError, match="Unknown workflow inputs"):
        resolve_workflow_inputs(
            definition,
            {"firmware": artifact, "expected_version": "1", "extra": "no"},
            artifact_resolver=resolver,
        )
    with pytest.raises(WorkflowInvalidError, match="must be an integer"):
        resolve_workflow_inputs(
            definition,
            {
                "firmware": artifact,
                "expected_version": "1",
                "ready_timeout": "soon",
            },
            artifact_resolver=resolver,
        )
    with pytest.raises(WorkflowInvalidError, match="must be a boolean"):
        resolve_workflow_inputs(
            definition,
            {
                "firmware": artifact,
                "expected_version": "1",
                "feature_enabled": "sometimes",
            },
            artifact_resolver=resolver,
        )


def test_phase4_expression_language_rejects_legacy_and_arbitrary_expressions() -> None:
    declared_legacy = parse_workflow_yaml(
        """
name: declared-legacy
version: 2
inputs: {pattern: {type: string, required: true}}
requirements: {capabilities: [serial]}
steps: [{action: assert_serial, pattern: "${pattern}"}]
"""
    )
    with pytest.raises(WorkflowInvalidError, match="only.*inputs"):
        resolve_workflow_inputs(declared_legacy, {"pattern": "READY"})

    arbitrary = parse_workflow_yaml(
        """
name: arbitrary
version: 2
inputs: {pattern: {type: string, default: READY}}
requirements: {capabilities: [serial]}
steps: [{action: assert_serial, pattern: "${{ env.SECRET }}"}]
"""
    )
    with pytest.raises(WorkflowInvalidError, match="only.*inputs"):
        resolve_workflow_inputs(arbitrary, None)

    empty_declarations = parse_workflow_yaml(
        """
name: empty-declarations
version: 2
inputs: {}
requirements: {capabilities: [serial]}
steps: [{action: assert_serial, pattern: "${pattern}"}]
"""
    )
    with pytest.raises(WorkflowInvalidError, match="Unknown workflow inputs"):
        resolve_workflow_inputs(empty_declarations, {"pattern": "READY"})
    with pytest.raises(WorkflowInvalidError, match="only.*inputs"):
        resolve_workflow_inputs(empty_declarations, None)

    legacy = parse_workflow_yaml(
        """
name: legacy
version: 1
requirements: {capabilities: [serial]}
steps: [{action: assert_serial, pattern: "^${pattern}$"}]
"""
    )
    resolved = resolve_workflow_inputs(legacy, {"pattern": "READY"})
    assert resolved.steps[0].pattern == "^READY$"  # type: ignore[union-attr]


def test_artifact_inputs_require_a_valid_resolver_and_exact_placeholder(tmp_path: Path) -> None:
    definition = parse_workflow_yaml(TYPED_WORKFLOW)
    artifact_id = uuid4()
    values = {
        "firmware": {"artifact_id": str(artifact_id)},
        "expected_version": "1.0.0",
    }
    with pytest.raises(WorkflowInvalidError, match="requires an artifact resolver"):
        resolve_workflow_inputs(definition, values)
    with pytest.raises(WorkflowInvalidError, match="artifact reference"):
        resolve_workflow_inputs(
            definition,
            values | {"firmware": {"artifact_id": "invalid"}},
            artifact_resolver=lambda _reference: tmp_path / "firmware.bin",
        )

    def invalid_path(_reference: ArtifactReference) -> Path:
        return "not-a-path"  # type: ignore[return-value]

    with pytest.raises(WorkflowInvalidError, match="invalid path"):
        resolve_workflow_inputs(
            definition,
            values,
            artifact_resolver=invalid_path,
        )

    embedded = parse_workflow_yaml(
        """
name: embedded-artifact
version: 2
inputs: {firmware: {type: artifact, required: true}}
requirements: {capabilities: [firmware]}
steps: [{action: flash, firmware: "prefix-${{ inputs.firmware }}"}]
"""
    )
    with pytest.raises(WorkflowInvalidError, match="entire value"):
        resolve_workflow_inputs(
            embedded,
            {"firmware": {"artifact_id": str(artifact_id)}},
            artifact_resolver=lambda _reference: tmp_path / "firmware.bin",
        )


def test_step_results_support_phase4_names_artifacts_and_statuses() -> None:
    artifact_ids = [uuid4(), uuid4()]
    pending = WorkflowStepResult(
        workflow_run_id=uuid4(),
        step_index=0,
        name="Flash firmware",
        action=WorkflowAction.FLASH,
        status=WorkflowStepStatus.PENDING,
        artifact_ids=artifact_ids,
    )
    skipped = pending.model_copy(update={"status": WorkflowStepStatus.SKIPPED})

    assert pending.started_at is None
    assert pending.artifact_ids == artifact_ids
    assert skipped.status is WorkflowStepStatus.SKIPPED
    assert pending.model_dump(mode="json")["name"] == "Flash firmware"


def test_declared_numeric_input_is_revalidated_in_its_step() -> None:
    definition = parse_workflow_yaml(
        """
name: invalid-timeout
version: 2
inputs: {timeout: {type: integer, required: true}}
requirements: {capabilities: [serial]}
steps:
  - action: read_serial
    timeout_seconds: "${{ inputs.timeout }}"
"""
    )
    with pytest.raises(WorkflowInvalidError, match="Resolved workflow inputs are invalid"):
        resolve_workflow_inputs(definition, {"timeout": -1})


def test_step_result_timestamp_still_normalizes_to_utc() -> None:
    result = WorkflowStepResult(
        workflow_run_id=uuid4(),
        step_index=1,
        name="Wait",
        action=WorkflowAction.WAIT,
        status=WorkflowStepStatus.SUCCEEDED,
        started_at=datetime(2026, 7, 23, 12, tzinfo=UTC),
    )
    assert result.started_at == datetime(2026, 7, 23, 12, tzinfo=UTC)


def test_workflow_service_accepts_a_per_run_artifact_resolver(tmp_path: Path) -> None:
    class Repository:
        def __init__(self) -> None:
            self.definition = parse_workflow_yaml(TYPED_WORKFLOW)
            self.run: object | None = None

        async def get_definition(self, name: str, version: int | None = None) -> object:
            return self.definition if name == self.definition.name else None

        async def create_run(self, run: object) -> object:
            self.run = run
            return run

    class Reservations:
        id = uuid4()

        async def require_active(self, bench_id: str, owner: str) -> object:
            return self.id

    class Backend:
        async def get_bench(self, bench_id: str) -> BenchSnapshot:
            return BenchSnapshot(
                id=bench_id,
                name=bench_id,
                status=BenchStatus.AVAILABLE,
                online=True,
                powered=True,
                capabilities=["firmware", "serial"],
            )

    class Backends:
        async def get_backend_for_bench(self, bench_id: str) -> Backend:
            return Backend()

    class Runner:
        def __init__(self) -> None:
            self.definition: WorkflowDefinition | None = None

        def schedule(self, definition: WorkflowDefinition, run: object) -> None:
            self.definition = definition

    async def scenario() -> None:
        repository = Repository()
        runner = Runner()
        service = WorkflowService(
            repository=repository,  # type: ignore[arg-type]
            backends=Backends(),  # type: ignore[arg-type]
            reservations=Reservations(),  # type: ignore[arg-type]
            runner=runner,  # type: ignore[arg-type]
        )
        artifact_id = uuid4()
        artifact_path = tmp_path / "authorized" / "firmware.bin"

        def resolver(reference: ArtifactReference) -> Path:
            assert reference.artifact_id == artifact_id
            return artifact_path

        await service.start(
            "esp32-ci-test",
            bench_id="bench-01",
            owner="ci",
            inputs={
                "firmware": {"artifact_id": str(artifact_id)},
                "expected_version": "0.5.0",
            },
            artifact_resolver=resolver,
        )

        scheduled = runner.definition
        assert scheduled is not None
        assert scheduled.steps[0].firmware == artifact_path  # type: ignore[union-attr]

    asyncio.run(scenario())
