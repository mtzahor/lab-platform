from __future__ import annotations

import asyncio
import importlib
import sys
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any, cast

import pytest
from lab_platform.models import Capability
from lab_platform.plugin_sdk import (
    BaseHardwarePlugin,
    DeviceDescriptor,
    DiagnosticCheck,
    DiagnosticStatus,
    PluginContext,
    PluginHealth,
    PluginHealthStatus,
    PluginMetadata,
    PluginRegistration,
    PluginRuntimeStatus,
)
from lab_platform.plugins import BasePlugin, PluginManager
from lab_platform.plugins.base import PluginFactory, PluginLoadError
from pydantic import BaseModel, ConfigDict

manager_module = importlib.import_module("lab_platform.plugins.manager")


class _EmptyConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class _RequiredConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    device: str


class _ManagedPlugin(BaseHardwarePlugin):
    def __init__(
        self,
        metadata: PluginMetadata,
        *,
        health_status: PluginHealthStatus = PluginHealthStatus.HEALTHY,
        health_error: Exception | None = None,
        health_waits: bool = False,
        initialize_error: Exception | None = None,
        initialize_waits: bool = False,
        shutdown_error: Exception | None = None,
        shutdown_waits: bool = False,
        descriptors: Sequence[DeviceDescriptor] = (),
        diagnostics: Sequence[DiagnosticCheck] = (),
        diagnostics_error: Exception | None = None,
    ) -> None:
        super().__init__(metadata)
        self.health_status = health_status
        self.health_error = health_error
        self.health_waits = health_waits
        self.initialize_error = initialize_error
        self.initialize_waits = initialize_waits
        self.shutdown_error = shutdown_error
        self.shutdown_waits = shutdown_waits
        self.descriptors = list(descriptors)
        self.diagnostic_checks = list(diagnostics)
        self.diagnostics_error = diagnostics_error
        self.shutdown_calls = 0

    async def initialize(self) -> None:
        if self.initialize_waits:
            await asyncio.Event().wait()
        if self.initialize_error is not None:
            raise self.initialize_error
        await super().initialize()

    async def health(self) -> PluginHealth:
        if self.health_waits:
            await asyncio.Event().wait()
        if self.health_error is not None:
            raise self.health_error
        return PluginHealth(status=self.health_status, message=self.health_status.value)

    async def discover(self) -> list[DeviceDescriptor]:
        return list(self.descriptors)

    async def diagnostics(self) -> list[DiagnosticCheck]:
        if self.diagnostics_error is not None:
            raise self.diagnostics_error
        return list(self.diagnostic_checks)

    async def shutdown(self) -> None:
        self.shutdown_calls += 1
        if self.shutdown_waits:
            await asyncio.Event().wait()
        if self.shutdown_error is not None:
            raise self.shutdown_error
        await super().shutdown()


class _VolatileMetadataPlugin(_ManagedPlugin):
    def __init__(self, metadata: PluginMetadata) -> None:
        super().__init__(metadata)
        self.metadata_error: Exception | None = None

    @property
    def metadata(self) -> PluginMetadata:
        if self.metadata_error is not None:
            raise self.metadata_error
        return super().metadata


class _EntryPoint:
    group = "lab_platform.plugins"

    def __init__(
        self,
        name: str,
        value: str,
        loader: Callable[[], Any],
        *,
        group: str = "lab_platform.plugins",
    ) -> None:
        self.name = name
        self.value = value
        self.group = group
        self._loader = loader

    def load(self) -> Any:
        return self._loader()


class _Distribution:
    def __init__(self, entry_points: Sequence[_EntryPoint]) -> None:
        self.entry_points = list(entry_points)


def _metadata(
    name: str,
    *,
    plugin_api_version: str = "1.0",
    minimum_agent_version: str = "0.9.0-beta",
    supported_platforms: list[str] | None = None,
) -> PluginMetadata:
    return PluginMetadata(
        name=name,
        version="1.0.0",
        plugin_api_version=plugin_api_version,
        description=f"{name} test plugin.",
        minimum_agent_version=minimum_agent_version,
        supported_platforms=supported_platforms or ["any"],
    )


def _registration(
    metadata: PluginMetadata,
    plugin: _ManagedPlugin,
    *,
    config_model: type[BaseModel] = _EmptyConfig,
) -> PluginRegistration[Any]:
    def create(_config: BaseModel, _context: PluginContext) -> _ManagedPlugin:
        return plugin

    return PluginRegistration(
        metadata=metadata,
        config_model=config_model,
        factory=create,
    )


def _legacy_factory(name: str) -> PluginFactory:
    return cast(
        PluginFactory,
        lambda: BasePlugin(
            _metadata(name),
            [Capability(name="probe")],
        ),
    )


def test_request_validation_disabled_plugins_and_unknown_diagnostics() -> None:
    with pytest.raises(ValueError, match="timeout must be positive"):
        PluginManager(lifecycle_timeout_seconds=0)

    async def scenario() -> None:
        manager = PluginManager(agent_version="1.0.0", platform="linux")
        report = await manager.load_resilient({"power": False})
        assert report.ok
        assert report.loaded == []
        runtime = manager.runtime_info()[0]
        assert runtime.status is PluginRuntimeStatus.DISABLED
        diagnostic = await manager.diagnose("power")
        assert diagnostic.checks[0].status is DiagnosticStatus.SKIPPED
        assert diagnostic.checks[0].remediation is None
        with pytest.raises(PluginLoadError, match="unknown"):
            await manager.diagnose("not-requested")

        invalid_requests: list[Any] = [
            {"power": {"enabled": "yes"}},
            {"power": {"config": [], "enabled": True}},
            {"power": {"config": {}, "unexpected": True}},
        ]
        for request in invalid_requests:
            with pytest.raises(ValueError):
                await manager.load_resilient(request)

    asyncio.run(scenario())


def test_resilient_loading_classifies_configuration_and_compatibility_failures() -> None:
    missing_config_metadata = _metadata("missing-config")
    missing_config_plugin = _ManagedPlugin(missing_config_metadata)
    incompatible_metadata = _metadata("future-api", plugin_api_version="2.0")
    incompatible_plugin = _ManagedPlugin(incompatible_metadata)
    future_agent_metadata = _metadata("future-agent", minimum_agent_version="2.0.0")
    future_agent_plugin = _ManagedPlugin(future_agent_metadata)
    wrong_platform_metadata = _metadata(
        "wrong-platform",
        supported_platforms=["windows"],
    )
    wrong_platform_plugin = _ManagedPlugin(wrong_platform_metadata)
    registration_metadata = _metadata("metadata-mismatch")
    mismatched_plugin = _ManagedPlugin(_metadata("different-name"))

    def exploding_factory() -> BasePlugin:
        raise RuntimeError("factory exploded")

    shared_metadata = _metadata("shared-name")
    providers: dict[str, PluginFactory | PluginRegistration[Any]] = {
        "missing-config": _registration(
            missing_config_metadata,
            missing_config_plugin,
            config_model=_RequiredConfig,
        ),
        "future-api": _registration(incompatible_metadata, incompatible_plugin),
        "future-agent": _registration(future_agent_metadata, future_agent_plugin),
        "wrong-platform": _registration(wrong_platform_metadata, wrong_platform_plugin),
        "metadata-mismatch": _registration(registration_metadata, mismatched_plugin),
        "legacy-config": _legacy_factory("legacy-config"),
        "factory-error": cast(PluginFactory, exploding_factory),
        "first-shared": _registration(shared_metadata, _ManagedPlugin(shared_metadata)),
        "second-shared": _registration(shared_metadata, _ManagedPlugin(shared_metadata)),
    }

    async def scenario() -> None:
        manager = PluginManager(
            providers,
            agent_version="1.0.0",
            platform="linux",
        )
        report = await manager.load_resilient(
            {
                "missing-config": None,
                "future-api": None,
                "future-agent": None,
                "wrong-platform": None,
                "metadata-mismatch": None,
                "legacy-config": {"unexpected": True},
                "factory-error": None,
                "first-shared": None,
                "second-shared": None,
            }
        )
        failures = {failure.plugin: failure for failure in report.failures}
        assert failures["missing-config"].code == "PLUGIN_CONFIGURATION_INVALID"
        assert failures["future-api"].code == "PLUGIN_INCOMPATIBLE"
        assert failures["future-agent"].code == "PLUGIN_INCOMPATIBLE"
        assert failures["wrong-platform"].code == "PLUGIN_INCOMPATIBLE"
        assert failures["metadata-mismatch"].code == "PLUGIN_CREATION_FAILED"
        assert failures["legacy-config"].code == "PLUGIN_CREATION_FAILED"
        assert failures["factory-error"].code == "PLUGIN_CREATION_FAILED"
        assert failures["shared-name"].code == "PLUGIN_DUPLICATE_NAME"
        assert report.loaded == ["shared-name"]
        incompatible = {item.name: item for item in manager.runtime_info()}
        assert incompatible["future-api"].status is PluginRuntimeStatus.INCOMPATIBLE
        assert incompatible["future-agent"].status is PluginRuntimeStatus.INCOMPATIBLE
        assert incompatible["wrong-platform"].status is PluginRuntimeStatus.INCOMPATIBLE
        await manager.shutdown()

    asyncio.run(scenario())


def test_discovery_conflicts_failures_direct_import_policy_and_invalid_directory(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    def broken_loader() -> Any:
        raise ImportError("entry point dependency unavailable")

    monkeypatch.setattr(
        manager_module,
        "entry_points",
        lambda **_kwargs: [
            _EntryPoint("power", "third_party:power", _legacy_factory("other-power")),
            _EntryPoint("broken-entry", "third_party:broken", broken_loader),
        ],
    )
    missing_directory = tmp_path / "missing"

    async def scenario() -> None:
        manager = PluginManager(
            plugin_directories=[missing_directory],
            allow_import_paths=False,
            agent_version="1.0.0",
        )
        report = await manager.load_resilient(
            ["power", "broken-entry", "missing.module:registration", "unknown"]
        )
        codes = {failure.code for failure in report.failures}
        assert {
            "PLUGIN_DISCOVERY_CONFLICT",
            "PLUGIN_DIRECTORY_INVALID",
            "PLUGIN_DISCOVERY_FAILED",
            "PLUGIN_IMPORT_PATH_DISABLED",
            "PLUGIN_NOT_FOUND",
        } <= codes
        assert report.loaded == ["power"]
        disabled_import = next(
            item for item in manager.runtime_info() if item.name == "missing.module:registration"
        )
        assert disabled_import.source == "import:missing.module:registration"
        await manager.shutdown()

        strict = PluginManager(agent_version="1.0.0")
        with pytest.raises(PluginLoadError, match="Could not discover plugin power"):
            strict.discover()

    asyncio.run(scenario())


def test_plugin_directory_entry_points_load_and_directory_errors_are_isolated(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    metadata = _metadata("directory-plugin")
    registration = _registration(metadata, _ManagedPlugin(metadata))
    entry = _EntryPoint("directory-plugin", "directory_plugin:registration", lambda: registration)
    monkeypatch.setattr(manager_module, "entry_points", lambda **_kwargs: [])
    monkeypatch.setattr(
        manager_module,
        "distributions",
        lambda **_kwargs: [
            _Distribution(
                [
                    entry,
                    _EntryPoint(
                        "ignored",
                        "ignored:registration",
                        lambda: registration,
                        group="unrelated.group",
                    ),
                ]
            )
        ],
    )

    async def scenario() -> None:
        manager = PluginManager(
            plugin_directories=[tmp_path],
            agent_version="1.0.0",
        )
        try:
            report = await manager.load_resilient(["directory-plugin"])
            assert report.ok
            assert report.loaded == ["directory-plugin"]
            info = manager.runtime_info()[0]
            assert info.source == "entry-point:directory_plugin:registration"
            await manager.shutdown()
        finally:
            while str(tmp_path) in sys.path:
                sys.path.remove(str(tmp_path))

    asyncio.run(scenario())

    def broken_distributions(**_kwargs: Any) -> Any:
        raise RuntimeError("metadata directory unreadable")

    monkeypatch.setattr(manager_module, "distributions", broken_distributions)
    manager = PluginManager(plugin_directories=[tmp_path], agent_version="1.0.0")
    try:
        report = asyncio.run(manager.load_resilient([]))
        assert report.failures[0].code == "PLUGIN_DIRECTORY_DISCOVERY_FAILED"
    finally:
        while str(tmp_path) in sys.path:
            sys.path.remove(str(tmp_path))


def test_lifecycle_timeouts_health_failures_and_shutdown_failures_are_isolated() -> None:
    init_timeout_metadata = _metadata("init-timeout")
    init_timeout = _ManagedPlugin(init_timeout_metadata, initialize_waits=True)
    init_failure_metadata = _metadata("init-failure")
    init_failure = _ManagedPlugin(
        init_failure_metadata,
        initialize_error=RuntimeError("initialization failed"),
        shutdown_error=RuntimeError("cleanup also failed"),
    )
    health_timeout_metadata = _metadata("health-timeout")
    health_timeout = _ManagedPlugin(health_timeout_metadata, health_waits=True)
    health_failure_metadata = _metadata("health-failure")
    health_failure = _ManagedPlugin(
        health_failure_metadata,
        health_error=RuntimeError("health probe failed"),
    )
    unhealthy_metadata = _metadata("unhealthy")
    unhealthy = _ManagedPlugin(
        unhealthy_metadata,
        health_status=PluginHealthStatus.UNHEALTHY,
    )
    shutdown_failure_metadata = _metadata("shutdown-failure")
    shutdown_failure = _ManagedPlugin(
        shutdown_failure_metadata,
        shutdown_error=RuntimeError("shutdown failed"),
    )
    shutdown_timeout_metadata = _metadata("shutdown-timeout")
    shutdown_timeout = _ManagedPlugin(shutdown_timeout_metadata, shutdown_waits=True)
    healthy_metadata = _metadata("healthy")
    healthy = _ManagedPlugin(healthy_metadata)
    plugins = [
        init_timeout,
        init_failure,
        health_timeout,
        health_failure,
        unhealthy,
        shutdown_failure,
        shutdown_timeout,
        healthy,
    ]
    providers = {plugin.metadata.name: _registration(plugin.metadata, plugin) for plugin in plugins}

    async def scenario() -> None:
        manager = PluginManager(
            providers,
            agent_version="1.0.0",
            lifecycle_timeout_seconds=0.001,
        )
        report = await manager.load_resilient(list(providers))
        codes = {failure.plugin: failure.code for failure in report.failures}
        assert codes == {
            "init-timeout": "PLUGIN_INITIALIZE_TIMEOUT",
            "init-failure": "PLUGIN_INITIALIZE_FAILED",
            "health-timeout": "PLUGIN_HEALTH_TIMEOUT",
            "health-failure": "PLUGIN_HEALTH_FAILED",
            "unhealthy": "PLUGIN_UNHEALTHY",
        }
        assert init_timeout.shutdown_calls == 1
        assert init_failure.shutdown_calls == 1
        runtime = {item.name: item.status for item in manager.runtime_info()}
        assert runtime["init-timeout"] is PluginRuntimeStatus.FAILED
        assert runtime["health-timeout"] is PluginRuntimeStatus.DEGRADED
        assert runtime["healthy"] is PluginRuntimeStatus.HEALTHY

        shutdown_failures = await manager.shutdown()
        assert {failure.plugin: failure.code for failure in shutdown_failures} == {
            "shutdown-timeout": "PLUGIN_SHUTDOWN_TIMEOUT",
            "shutdown-failure": "PLUGIN_SHUTDOWN_FAILED",
        }
        stopped = {item.name: item.status for item in manager.runtime_info()}
        assert stopped["healthy"] is PluginRuntimeStatus.STOPPED
        assert manager.plugins() == []

    asyncio.run(scenario())


def test_cached_metadata_isolates_diagnostics_and_shutdown_from_plugin_property_failures() -> None:
    volatile_metadata = _metadata("a-volatile-metadata")
    volatile = _VolatileMetadataPlugin(volatile_metadata)
    healthy_metadata = _metadata("z-healthy")
    healthy = _ManagedPlugin(healthy_metadata)
    providers = {
        volatile_metadata.name: _registration(volatile_metadata, volatile),
        healthy_metadata.name: _registration(healthy_metadata, healthy),
    }

    async def scenario() -> None:
        manager = PluginManager(providers, agent_version="1.0.0")
        report = await manager.load_resilient(list(providers))
        assert report.ok

        volatile.metadata_error = RuntimeError("metadata became unavailable")

        diagnostics = {item.plugin: item for item in await manager.diagnose_all()}
        assert set(diagnostics) == {"a-volatile-metadata", "z-healthy"}
        assert diagnostics["a-volatile-metadata"].metadata == volatile_metadata
        assert diagnostics["a-volatile-metadata"].status is PluginRuntimeStatus.HEALTHY

        assert await manager.shutdown() == ()
        assert volatile.shutdown_calls == 1
        assert healthy.shutdown_calls == 1
        stopped = {item.name: item for item in manager.runtime_info()}
        assert stopped["a-volatile-metadata"].metadata == volatile_metadata
        assert stopped["a-volatile-metadata"].status is PluginRuntimeStatus.STOPPED
        assert stopped["z-healthy"].status is PluginRuntimeStatus.STOPPED

    asyncio.run(scenario())


def test_diagnostics_report_health_discovery_and_provider_failures() -> None:
    descriptor = DeviceDescriptor(
        id="target-01",
        name="Target",
        type="target",
        capabilities={"probe"},
    )
    warning_metadata = _metadata("warning")
    warning = _ManagedPlugin(
        warning_metadata,
        health_status=PluginHealthStatus.DEGRADED,
        descriptors=[descriptor],
        diagnostics=[
            DiagnosticCheck(
                name="permissions",
                status=DiagnosticStatus.WARNING,
                message="udev rule is recommended",
            )
        ],
    )
    duplicate_metadata = _metadata("duplicate-devices")
    duplicate_devices = _ManagedPlugin(
        duplicate_metadata,
        descriptors=[descriptor, descriptor],
    )
    health_error_metadata = _metadata("diagnostic-health-error")
    health_error = _ManagedPlugin(
        health_error_metadata,
        health_error=RuntimeError("health unavailable"),
    )
    diagnostics_error_metadata = _metadata("diagnostics-error")
    diagnostics_error = _ManagedPlugin(
        diagnostics_error_metadata,
        diagnostics_error=RuntimeError("custom diagnostics failed"),
    )
    providers = {
        plugin.metadata.name: _registration(plugin.metadata, plugin)
        for plugin in (warning, duplicate_devices, health_error, diagnostics_error)
    }

    async def scenario() -> None:
        manager = PluginManager(providers, agent_version="1.0.0")
        await manager.load_resilient(list(providers))
        reports = {report.plugin: report for report in await manager.diagnose_all()}

        warning_report = reports["warning"]
        assert warning_report.status is PluginRuntimeStatus.DEGRADED
        assert warning_report.devices == [descriptor]
        assert {check.name for check in warning_report.checks} == {
            "compatibility",
            "health",
            "discovery",
            "permissions",
        }
        runtime = {item.name: item for item in manager.runtime_info()}
        assert runtime["warning"].device_count == 1

        duplicate = reports["duplicate-devices"]
        assert duplicate.status is PluginRuntimeStatus.DEGRADED
        assert (
            next(check for check in duplicate.checks if check.name == "discovery").status
            is DiagnosticStatus.FAIL
        )
        assert (
            next(
                check
                for check in reports["diagnostic-health-error"].checks
                if check.name == "health"
            ).message
            == "health unavailable"
        )
        assert (
            next(
                check
                for check in reports["diagnostics-error"].checks
                if check.name == "plugin diagnostics"
            ).status
            is DiagnosticStatus.FAIL
        )
        await manager.shutdown()

    asyncio.run(scenario())


def test_strict_loader_preserves_legacy_contract_and_rolls_back_on_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(manager_module, "entry_points", lambda **_kwargs: [])

    async def scenario() -> None:
        manager = PluginManager(agent_version="1.0.0")
        loaded = await manager.load(["power"])
        assert [plugin.metadata.name for plugin in loaded] == ["power"]
        assert cast(BasePlugin, loaded[0]).initialized
        await manager.shutdown()

        duplicate = PluginManager(agent_version="1.0.0")
        with pytest.raises(PluginLoadError, match="Duplicate plugin"):
            await duplicate.load(["power", "power"])
        assert duplicate.plugins() == []

        missing = PluginManager(allow_import_paths=False, agent_version="1.0.0")
        with pytest.raises(PluginLoadError, match="import-path plugins are disabled"):
            await missing.load(["missing.module:plugin"])

    asyncio.run(scenario())


def test_strict_discover_wraps_entry_point_load_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def broken_loader() -> Any:
        raise ImportError("missing dependency")

    monkeypatch.setattr(
        manager_module,
        "entry_points",
        lambda **_kwargs: [_EntryPoint("broken-entry", "broken:registration", broken_loader)],
    )
    manager = PluginManager(agent_version="1.0.0")
    with pytest.raises(PluginLoadError, match="Could not discover plugin broken-entry"):
        manager.discover()
