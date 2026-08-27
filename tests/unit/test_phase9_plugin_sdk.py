from __future__ import annotations

import asyncio
import importlib
import sys
from pathlib import Path

import pytest
from lab_platform.plugin_sdk import (
    BaseHardwarePlugin,
    CompatibilityStatus,
    DeviceDescriptor,
    DeviceDriver,
    DeviceUnavailableError,
    FirmwareArtifact,
    FlashOptions,
    HardwarePlugin,
    PluginApiVersion,
    PluginContext,
    PluginLoadReport,
    PluginMetadata,
    PluginOperationError,
    PluginRegistration,
    ProgressUpdate,
    SemanticVersion,
    UnsupportedCapabilityError,
    assert_cancellation_propagates,
    assert_plugin_contract,
    assert_timeout_enforced,
    assess_plugin_compatibility,
    create_plugin_scaffold,
    normalize_platform,
)
from lab_platform.plugin_sdk.cli import main as plugin_cli_main
from lab_platform.plugin_sdk.testing import FakeDeviceDriver, FakeFlashCapability
from lab_platform.plugins import PluginManager
from pydantic import BaseModel, ConfigDict, ValidationError


class _EmptyConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class _DuplicateDevicePlugin(BaseHardwarePlugin):
    async def discover(self) -> list[DeviceDescriptor]:
        descriptor = DeviceDescriptor(
            id="duplicate",
            name="Duplicate",
            type="test",
        )
        return [descriptor, descriptor]


async def _get_driver(plugin: HardwarePlugin, device_id: str) -> DeviceDriver:
    """Invoke through the stable protocol instead of a concrete always-raising fixture."""

    return await plugin.get_driver(device_id)


def _duplicate_registration() -> PluginRegistration[_EmptyConfig]:
    metadata = PluginMetadata(
        name="duplicate-device-plugin",
        version="1.0.0",
        description="Contract failure fixture.",
    )

    def create(
        _config: _EmptyConfig,
        _context: PluginContext,
    ) -> _DuplicateDevicePlugin:
        return _DuplicateDevicePlugin(metadata)

    return PluginRegistration(
        metadata=metadata,
        config_model=_EmptyConfig,
        factory=create,
    )


def test_metadata_and_compatibility_fail_cleanly_at_stable_boundaries() -> None:
    metadata = PluginMetadata(
        name="reference-plugin",
        version="1.2.3",
        plugin_api_version="1.0",
        description="Reference contract plugin.",
        capabilities=["firmware", "serial"],
        minimum_agent_version="0.9.0-beta",
        maximum_agent_version="1.9.9",
    )
    assert metadata.capabilities == ["flash", "serial"]
    compatible = assess_plugin_compatibility(
        metadata,
        agent_version="1.0.0",
        supported_plugin_api_version="1.4",
        platform="linux",
    )
    assert compatible.compatible

    incompatible = assess_plugin_compatibility(
        metadata.model_copy(update={"plugin_api_version": "2.0"}),
        agent_version="1.0.0",
        supported_plugin_api_version="1.4",
        platform="linux",
    )
    assert incompatible.status is CompatibilityStatus.PLUGIN_API_INCOMPATIBLE
    assert "This Agent supports Plugin API 1.4" in incompatible.message

    with pytest.raises(ValidationError, match="maximum_agent_version"):
        PluginMetadata(
            name="invalid-range",
            version="1.0.0",
            description="Invalid version range.",
            minimum_agent_version="2.0.0",
            maximum_agent_version="1.0.0",
        )


def test_generated_plugin_is_importable_and_passes_the_sdk_contract(tmp_path: Path) -> None:
    root = create_plugin_scaffold("example-relay", tmp_path)
    assert (root / "pyproject.toml").is_file()
    assert (root / "tests/test_contract.py").is_file()
    sys.path.insert(0, str(root / "src"))
    try:
        module = importlib.import_module("example_relay.plugin")
        asyncio.run(assert_plugin_contract(module.registration))
    finally:
        sys.path.remove(str(root / "src"))
        for name in tuple(sys.modules):
            if name == "example_relay" or name.startswith("example_relay."):
                sys.modules.pop(name, None)


def test_timeout_and_cancellation_contract_helpers() -> None:
    async def waits_forever() -> None:
        await asyncio.Event().wait()

    async def scenario() -> None:
        await assert_cancellation_propagates(waits_forever)
        await assert_timeout_enforced(waits_forever, timeout_seconds=0.001)

    asyncio.run(scenario())


def test_official_plugins_are_discoverable_without_loading_optional_tools() -> None:
    manager = PluginManager(agent_version="0.9.0-beta", platform="linux")
    discovered = manager.discover()

    assert {
        "esp32",
        "openocd",
        "jlink",
        "rp2040",
        "nrf52",
        "usb-relay",
        "network-relay",
        "socketcan",
        "instruments",
    }.issubset(discovered)


def test_semantic_and_plugin_api_versions_enforce_stable_boundaries() -> None:
    assert SemanticVersion.parse("v1.2.3-beta.2") < SemanticVersion.parse("1.2.3-rc.1")
    assert SemanticVersion.parse("1.2.3-rc.1") < SemanticVersion.parse("1.2.3")
    assert SemanticVersion.parse("1.2.3+build.7") == SemanticVersion.parse("1.2.3")
    with pytest.raises(ValueError, match="semantic version"):
        SemanticVersion.parse("1.2")
    with pytest.raises(TypeError):
        _ = SemanticVersion.parse("1.0.0") < object()

    assert PluginApiVersion.parse("1.9").is_compatible_with(PluginApiVersion.parse("1.0"))
    assert not PluginApiVersion.parse("2.0").is_compatible_with(PluginApiVersion.parse("1.99"))
    with pytest.raises(ValueError, match="Plugin API version"):
        PluginApiVersion.parse("v1.0")

    assert normalize_platform("linux2") == "linux"
    assert normalize_platform("darwin") == "macos"
    assert normalize_platform("MSYS_NT") == "windows"
    assert normalize_platform("freebsd") == "freebsd"


def test_compatibility_reports_agent_platform_and_invalid_metadata_failures() -> None:
    metadata = PluginMetadata(
        name="bounded-plugin",
        version="1.0.0",
        description="Compatibility boundary fixture.",
        minimum_agent_version="1.0.0",
        maximum_agent_version="1.5.0",
        supported_platforms=["Linux"],
    )
    too_old = assess_plugin_compatibility(
        metadata,
        agent_version="0.9.9",
        platform="linux",
    )
    too_new = assess_plugin_compatibility(
        metadata,
        agent_version="2.0.0",
        platform="linux",
    )
    wrong_platform = assess_plugin_compatibility(
        metadata,
        agent_version="1.2.0",
        platform="macos",
    )
    assert too_old.status is CompatibilityStatus.AGENT_VERSION_INCOMPATIBLE
    assert too_new.status is CompatibilityStatus.AGENT_VERSION_INCOMPATIBLE
    assert wrong_platform.status is CompatibilityStatus.PLATFORM_UNSUPPORTED
    assert "supported: linux" in wrong_platform.message

    invalid = metadata.model_construct(plugin_api_version="invalid")
    result = assess_plugin_compatibility(
        invalid,
        agent_version="1.2.0",
        platform="linux",
    )
    assert result.status is CompatibilityStatus.INVALID_METADATA
    assert not result.compatible


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("supported_platforms", ["linux", "LINUX"], "duplicate supported platform"),
        ("supported_devices", ["board", "board"], "duplicate supported device"),
        ("capabilities", ["firmware", "flash"], "duplicate capability"),
        ("capabilities", ["not valid"], "invalid capability"),
        ("supported_platforms", [""], "non-empty strings"),
    ],
)
def test_metadata_rejects_ambiguous_or_unsafe_lists(
    field: str,
    value: list[str],
    message: str,
) -> None:
    raw = {
        "name": "validation-plugin",
        "version": "1.0.0",
        "description": "Metadata validation fixture.",
        field: value,
    }
    with pytest.raises(ValidationError, match=message):
        PluginMetadata.model_validate(raw)


def test_sdk_base_driver_registration_and_error_contracts(tmp_path: Path) -> None:
    metadata = PluginMetadata(
        name="base-plugin",
        version="v1.0.0",
        description="Base lifecycle fixture.",
    )
    plugin = BaseHardwarePlugin(metadata)

    async def scenario() -> None:
        assert (await plugin.health()).status.value == "unknown"
        await plugin.initialize()
        assert bool(plugin.initialized)
        assert (await plugin.health()).status.value == "healthy"
        assert await plugin.discover() == []
        await plugin.shutdown()
        assert not bool(plugin.initialized)

        descriptor = DeviceDescriptor(
            id="fake-01",
            name="Fake",
            type="fake",
            capabilities={"Firmware"},
        )
        flash = FakeFlashCapability(updates=[ProgressUpdate(percent=50, message="half")])
        driver = FakeDeviceDriver(descriptor, {"flash": flash})
        assert driver.capabilities == {"flash"}
        assert (await driver.health()).status.value == "healthy"
        assert await driver.get_capability("FLASH") is flash
        with pytest.raises(UnsupportedCapabilityError) as unsupported:
            await driver.get_capability("serial")
        assert unsupported.value.details == {"capability": "serial"}

        artifact = FirmwareArtifact(
            filename="firmware.bin",
            local_path=tmp_path / "firmware.bin",
            sha256="0" * 64,
            size_bytes=1,
        )
        updates = [update async for update in flash.flash(artifact, FlashOptions(verify=False))]
        assert updates[0].percent == 50
        assert flash.calls[0][1].verify is False
        with pytest.raises(DeviceUnavailableError) as unavailable:
            await _get_driver(plugin, "missing")
        assert unavailable.value.details == {"device_id": "missing"}

    asyncio.run(scenario())
    assert metadata.version == "1.0.0"

    error = PluginOperationError("operation failed", device="fake-01")
    assert error.code == "PLUGIN_OPERATION_FAILED"
    assert error.message == "operation failed"
    assert error.details == {"device": "fake-01"}
    assert str(error) == "operation failed"
    assert PluginLoadReport().ok


def test_registration_rejects_extra_config_and_metadata_mismatch() -> None:
    metadata = PluginMetadata(
        name="registration",
        version="1.0.0",
        description="Registration validation fixture.",
    )
    wrong = BaseHardwarePlugin(
        PluginMetadata(
            name="wrong",
            version="1.0.0",
            description="Mismatched plugin.",
        )
    )
    registration = PluginRegistration(
        metadata=metadata,
        config_model=_EmptyConfig,
        factory=lambda _config, _context: wrong,
    )
    with pytest.raises(ValidationError):
        registration.validate_config({"unexpected": True})
    with pytest.raises(ValueError, match="metadata does not match"):
        registration.create(
            registration.validate_config(),
            PluginContext(
                logger=importlib.import_module("logging").getLogger("test"),
                agent_version="1.0.0",
            ),
        )


def test_contract_helpers_explain_broken_plugins_and_operations() -> None:
    async def completes_immediately() -> None:
        return None

    async def swallows_cancellation() -> None:
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            return

    async def scenario() -> None:
        with pytest.raises(AssertionError, match="device IDs must be unique"):
            await assert_plugin_contract(_duplicate_registration())
        with pytest.raises(AssertionError, match="before cancellation"):
            await assert_cancellation_propagates(completes_immediately)
        with pytest.raises(AssertionError, match="swallowed"):
            await assert_cancellation_propagates(swallows_cancellation)
        with pytest.raises(ValueError, match="must be positive"):
            await assert_timeout_enforced(completes_immediately, timeout_seconds=0)
        with pytest.raises(AssertionError, match="without exercising"):
            await assert_timeout_enforced(completes_immediately)

    asyncio.run(scenario())


def test_scaffold_cli_reports_success_and_actionable_errors(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert plugin_cli_main(["init", "cli-relay", "--directory", str(tmp_path)]) == 0
    assert "Created plugin project" in capsys.readouterr().out

    assert plugin_cli_main(["init", "invalid name", "--directory", str(tmp_path)]) == 1
    assert "plugin name must start" in capsys.readouterr().err

    assert plugin_cli_main(["init", "cli-relay", "--directory", str(tmp_path)]) == 1
    assert "error:" in capsys.readouterr().err

    with pytest.raises(ValueError, match="plugin name must start"):
        create_plugin_scaffold("9-invalid", tmp_path)
