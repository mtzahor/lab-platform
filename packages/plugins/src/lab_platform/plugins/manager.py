from __future__ import annotations

import asyncio
import importlib
import logging
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from importlib.metadata import distributions, entry_points
from pathlib import Path
from typing import Any, Literal, cast

from lab_platform.plugin_sdk import (
    ENTRY_POINT_GROUP,
    PLUGIN_API_VERSION,
    DiagnosticCheck,
    DiagnosticStatus,
    PluginCompatibility,
    PluginCompatibilityError,
    PluginContext,
    PluginDiagnosticReport,
    PluginFailure,
    PluginHealth,
    PluginHealthStatus,
    PluginLoadReport,
    PluginMetadata,
    PluginRegistration,
    PluginRuntimeInfo,
    PluginRuntimeStatus,
    assess_plugin_compatibility,
)
from lab_platform.plugins.base import Plugin, PluginFactory, PluginLoadError
from lab_platform.plugins.builtins import BUILTIN_PLUGINS
from pydantic import ValidationError

PluginProvider = PluginFactory | PluginRegistration[Any]
PluginConfiguration = Sequence[str] | Mapping[str, Mapping[str, Any] | bool | None]


@dataclass(frozen=True, slots=True)
class _Candidate:
    name: str
    source: str
    provider: PluginProvider | None = None
    entry_point: Any | None = None

    def load(self) -> PluginProvider:
        if self.provider is not None:
            return self.provider
        if self.entry_point is None:  # pragma: no cover - construction invariant
            raise PluginLoadError(f"Plugin candidate {self.name!r} has no loader")
        return cast(PluginProvider, self.entry_point.load())


@dataclass(frozen=True, slots=True)
class _RequestedPlugin:
    name: str
    enabled: bool
    config: dict[str, Any]


class PluginManager:
    """Discover and supervise both legacy and Plugin API 1.x plugins.

    ``load`` retains the pre-1.0 all-or-nothing contract. ``load_resilient`` is
    the production path: one broken or incompatible plugin is reported without
    taking healthy plugins down with it.
    """

    def __init__(
        self,
        available_plugins: Mapping[str, PluginProvider] | None = None,
        *,
        plugin_directories: Sequence[str | Path] = (),
        allow_import_paths: bool = True,
        agent_version: str | None = None,
        plugin_api_version: str = PLUGIN_API_VERSION,
        lifecycle_timeout_seconds: float = 30.0,
        platform: str | None = None,
    ) -> None:
        if lifecycle_timeout_seconds <= 0:
            raise ValueError("plugin lifecycle timeout must be positive")
        self._available_plugins: dict[str, PluginProvider] = dict(BUILTIN_PLUGINS)
        if available_plugins is not None:
            self._available_plugins.update(available_plugins)
        self._plugin_directories = tuple(
            Path(item).expanduser().resolve() for item in plugin_directories
        )
        self._allow_import_paths = allow_import_paths
        self._agent_version = agent_version or _running_agent_version()
        self._plugin_api_version = plugin_api_version
        self._lifecycle_timeout_seconds = lifecycle_timeout_seconds
        self._platform = platform
        self._plugins: dict[str, Plugin] = {}
        self._runtime: dict[str, PluginRuntimeInfo] = {}
        self._sources: dict[str, str] = {}
        self._logger = logging.getLogger("lab-platform.plugins")

    def discover(self) -> dict[str, PluginProvider]:
        """Return available providers and fail strictly on discovery problems."""

        candidates, failures = self._discover_candidates()
        if failures:
            failure = failures[0]
            raise PluginLoadError(f"Could not discover plugin {failure.plugin}: {failure.message}")
        discovered: dict[str, PluginProvider] = {}
        for name, candidate in sorted(candidates.items()):
            try:
                discovered[name] = candidate.load()
            except Exception as exc:  # noqa: BLE001 - third-party import boundary
                raise PluginLoadError(f"Could not discover plugin {name}") from exc
        return discovered

    async def load(self, plugin_names: list[str]) -> list[Plugin]:
        """Strict compatibility loader retained for existing callers and tests."""

        discovered = self.discover()
        loaded: list[Plugin] = []
        plugin_name = "unknown"
        try:
            for plugin_name in plugin_names:
                provider = discovered.get(plugin_name)
                source = f"configured:{plugin_name}"
                if provider is None:
                    provider = self._load_import_path_provider(plugin_name)
                    source = f"import:{plugin_name}"
                plugin, metadata = self._create_plugin(provider, {}, source=source)
                if metadata.name in self._plugins:
                    raise PluginLoadError(f"Duplicate plugin: {metadata.name}")
                self._set_runtime(
                    metadata.name,
                    source,
                    PluginRuntimeStatus.INITIALIZING,
                    metadata=metadata,
                )
                await self._initialize(plugin, metadata.name)
                self._plugins[metadata.name] = plugin
                self._sources[metadata.name] = source
                self._set_runtime(
                    metadata.name,
                    source,
                    PluginRuntimeStatus.HEALTHY,
                    metadata=metadata,
                )
                loaded.append(plugin)
        except asyncio.CancelledError:
            await self.shutdown()
            raise
        except Exception as exc:
            await self.shutdown()
            if isinstance(exc, PluginLoadError):
                raise
            raise PluginLoadError(f"Could not initialize plugin {plugin_name}") from exc
        return loaded

    async def load_resilient(self, requested: PluginConfiguration) -> PluginLoadReport:
        """Load configured plugins independently and return every observed failure."""

        specifications = _requested_plugins(requested)
        candidates, discovery_failures = self._discover_candidates()
        failures = list(discovery_failures)
        loaded: list[str] = []

        for specification in specifications:
            if not specification.enabled:
                self._set_runtime(
                    specification.name,
                    f"configured:{specification.name}",
                    PluginRuntimeStatus.DISABLED,
                )
                continue
            candidate = candidates.get(specification.name)
            if candidate is None and ":" in specification.name:
                if not self._allow_import_paths:
                    failure = PluginFailure(
                        plugin=specification.name,
                        stage="discovery",
                        code="PLUGIN_IMPORT_PATH_DISABLED",
                        message=(
                            "Direct module:attribute plugin imports are disabled; install the "
                            f"plugin with a {ENTRY_POINT_GROUP!r} entry point."
                        ),
                    )
                    failures.append(failure)
                    self._failure_runtime(failure, f"import:{specification.name}")
                    continue
                candidate = _Candidate(
                    specification.name,
                    f"import:{specification.name}",
                    provider=self._load_import_path_provider_safely(
                        specification.name,
                        failures,
                    ),
                )
                if candidate.provider is None:
                    self._failure_runtime(failures[-1], candidate.source)
                    continue
            if candidate is None:
                failure = PluginFailure(
                    plugin=specification.name,
                    stage="discovery",
                    code="PLUGIN_NOT_FOUND",
                    message=f"Unknown plugin: {specification.name}",
                )
                failures.append(failure)
                self._failure_runtime(failure, f"configured:{specification.name}")
                continue

            try:
                provider = candidate.load()
            except Exception as exc:  # noqa: BLE001 - third-party import boundary
                failure = _plugin_failure(
                    specification.name,
                    "discovery",
                    "PLUGIN_DISCOVERY_FAILED",
                    exc,
                )
                failures.append(failure)
                self._failure_runtime(failure, candidate.source)
                continue

            try:
                plugin, metadata = self._create_plugin(
                    provider,
                    specification.config,
                    source=candidate.source,
                )
            except ValidationError as exc:
                failure = _plugin_failure(
                    specification.name,
                    "configuration",
                    "PLUGIN_CONFIGURATION_INVALID",
                    exc,
                )
                failures.append(failure)
                self._failure_runtime(failure, candidate.source)
                continue
            except PluginCompatibilityError as exc:
                failure = _plugin_failure(
                    specification.name,
                    "compatibility",
                    "PLUGIN_INCOMPATIBLE",
                    exc,
                )
                failures.append(failure)
                self._failure_runtime(
                    failure,
                    candidate.source,
                    status=PluginRuntimeStatus.INCOMPATIBLE,
                )
                continue
            except Exception as exc:  # noqa: BLE001 - third-party factory boundary
                failure = _plugin_failure(
                    specification.name,
                    "configuration",
                    "PLUGIN_CREATION_FAILED",
                    exc,
                )
                failures.append(failure)
                self._failure_runtime(failure, candidate.source)
                continue

            if metadata.name in self._plugins:
                failure = PluginFailure(
                    plugin=metadata.name,
                    stage="configuration",
                    code="PLUGIN_DUPLICATE_NAME",
                    message=f"Duplicate plugin metadata name: {metadata.name}",
                )
                failures.append(failure)
                self._failure_runtime(failure, candidate.source)
                continue
            self._set_runtime(
                metadata.name,
                candidate.source,
                PluginRuntimeStatus.INITIALIZING,
                metadata=metadata,
            )
            try:
                await self._initialize(plugin, metadata.name)
            except Exception as exc:  # noqa: BLE001 - isolated lifecycle boundary
                code = (
                    "PLUGIN_INITIALIZE_TIMEOUT"
                    if isinstance(exc, TimeoutError)
                    else "PLUGIN_INITIALIZE_FAILED"
                )
                failure = _plugin_failure(metadata.name, "initialize", code, exc)
                failures.append(failure)
                self._failure_runtime(failure, candidate.source, metadata=metadata)
                await self._shutdown_untracked(plugin, metadata.name)
                continue

            self._plugins[metadata.name] = plugin
            self._sources[metadata.name] = candidate.source
            status, health_failure = await self._initial_health(plugin, metadata)
            if health_failure is not None:
                failures.append(health_failure)
            self._set_runtime(
                metadata.name,
                candidate.source,
                status,
                metadata=metadata,
            )
            loaded.append(metadata.name)
        return PluginLoadReport(loaded=loaded, failures=failures)

    async def shutdown(self) -> tuple[PluginFailure, ...]:
        failures: list[PluginFailure] = []
        for name, plugin in reversed(list(self._plugins.items())):
            source = self._sources.get(name, f"plugin:{name}")
            info = self._runtime.get(name)
            metadata = info.metadata if info is not None else None
            try:
                await asyncio.wait_for(
                    plugin.shutdown(),
                    timeout=self._lifecycle_timeout_seconds,
                )
                self._set_runtime(
                    name,
                    source,
                    PluginRuntimeStatus.STOPPED,
                    metadata=metadata,
                )
            except Exception as exc:  # noqa: BLE001 - shutdown must continue
                code = (
                    "PLUGIN_SHUTDOWN_TIMEOUT"
                    if isinstance(exc, TimeoutError)
                    else "PLUGIN_SHUTDOWN_FAILED"
                )
                failure = _plugin_failure(name, "shutdown", code, exc)
                failures.append(failure)
                self._failure_runtime(failure, source, metadata=metadata)
                self._logger.warning(
                    "Plugin shutdown failed",
                    extra={"plugin": name, "error": str(exc)},
                )
        self._plugins.clear()
        self._sources.clear()
        return tuple(failures)

    def plugins(self) -> list[Plugin]:
        return [self._plugins[name] for name in sorted(self._plugins)]

    def runtime_info(self) -> list[PluginRuntimeInfo]:
        return [self._runtime[name] for name in sorted(self._runtime)]

    async def diagnose(self, plugin_name: str) -> PluginDiagnosticReport:
        plugin = self._plugins.get(plugin_name)
        info = self._runtime.get(plugin_name)
        if info is None:
            raise PluginLoadError(f"Plugin {plugin_name!r} is unknown")
        if plugin is None:
            return PluginDiagnosticReport(
                plugin=plugin_name,
                status=info.status,
                metadata=info.metadata,
                checks=[
                    DiagnosticCheck(
                        name="lifecycle",
                        status=(
                            DiagnosticStatus.SKIPPED
                            if info.status is PluginRuntimeStatus.DISABLED
                            else DiagnosticStatus.FAIL
                        ),
                        message=info.error_message or f"Plugin is {info.status.value}",
                        remediation=(
                            None
                            if info.status is PluginRuntimeStatus.DISABLED
                            else "Review plugin configuration, compatibility, and dependencies."
                        ),
                        details={"error_code": info.error_code}
                        if info.error_code is not None
                        else {},
                    )
                ],
            )
        metadata = info.metadata
        if metadata is None:  # pragma: no cover - loaded plugin invariant
            raise PluginLoadError(f"Plugin {plugin_name!r} has no validated runtime metadata")
        checks = [
            DiagnosticCheck(
                name="compatibility",
                status=DiagnosticStatus.PASS,
                message=self._compatibility(metadata).message,
            )
        ]
        devices: list[Any] = []
        status = info.status

        health_method = getattr(plugin, "health", None)
        if callable(health_method):
            try:
                health = cast(
                    PluginHealth,
                    await asyncio.wait_for(
                        health_method(),
                        timeout=self._lifecycle_timeout_seconds,
                    ),
                )
                health_status = (
                    DiagnosticStatus.PASS
                    if health.status is PluginHealthStatus.HEALTHY
                    else DiagnosticStatus.WARNING
                    if health.status is PluginHealthStatus.DEGRADED
                    else DiagnosticStatus.FAIL
                )
                checks.append(
                    DiagnosticCheck(
                        name="health",
                        status=health_status,
                        message=health.message or health.status.value,
                        details=health.details,
                    )
                )
                if health_status is not DiagnosticStatus.PASS:
                    status = PluginRuntimeStatus.DEGRADED
            except Exception as exc:  # noqa: BLE001 - diagnostic boundary
                checks.append(
                    DiagnosticCheck(
                        name="health",
                        status=DiagnosticStatus.FAIL,
                        message=str(exc) or type(exc).__name__,
                    )
                )
                status = PluginRuntimeStatus.DEGRADED

        discover_method = getattr(plugin, "discover", None)
        if callable(discover_method):
            try:
                devices = list(
                    await asyncio.wait_for(
                        discover_method(),
                        timeout=self._lifecycle_timeout_seconds,
                    )
                )
                identifiers = [device.id for device in devices]
                if len(identifiers) != len(set(identifiers)):
                    raise ValueError("plugin returned duplicate device IDs")
                checks.append(
                    DiagnosticCheck(
                        name="discovery",
                        status=DiagnosticStatus.PASS,
                        message=f"Detected {len(devices)} device(s)",
                    )
                )
            except Exception as exc:  # noqa: BLE001 - diagnostic boundary
                checks.append(
                    DiagnosticCheck(
                        name="discovery",
                        status=DiagnosticStatus.FAIL,
                        message=str(exc) or type(exc).__name__,
                    )
                )
                status = PluginRuntimeStatus.DEGRADED

        diagnostics_method = getattr(plugin, "diagnostics", None)
        if callable(diagnostics_method):
            try:
                checks.extend(
                    await asyncio.wait_for(
                        diagnostics_method(),
                        timeout=self._lifecycle_timeout_seconds,
                    )
                )
            except Exception as exc:  # noqa: BLE001 - diagnostic boundary
                checks.append(
                    DiagnosticCheck(
                        name="plugin diagnostics",
                        status=DiagnosticStatus.FAIL,
                        message=str(exc) or type(exc).__name__,
                    )
                )
                status = PluginRuntimeStatus.DEGRADED

        has_failed_check = any(check.status is DiagnosticStatus.FAIL for check in checks)
        has_warning_check = any(check.status is DiagnosticStatus.WARNING for check in checks)
        if has_failed_check or (status is PluginRuntimeStatus.HEALTHY and has_warning_check):
            status = PluginRuntimeStatus.DEGRADED

        self._set_runtime(
            metadata.name,
            info.source,
            status,
            metadata=metadata,
            device_count=len(devices),
        )
        return PluginDiagnosticReport(
            plugin=metadata.name,
            status=status,
            metadata=metadata,
            checks=checks,
            devices=devices,
        )

    async def diagnose_all(self) -> list[PluginDiagnosticReport]:
        reports: list[PluginDiagnosticReport] = []
        for name in sorted(self._runtime):
            reports.append(await self.diagnose(name))
        return reports

    def _discover_candidates(self) -> tuple[dict[str, _Candidate], list[PluginFailure]]:
        candidates = {
            name: _Candidate(name, f"builtin:{name}", provider=provider)
            for name, provider in self._available_plugins.items()
        }
        failures: list[PluginFailure] = []

        try:
            installed = list(entry_points(group=ENTRY_POINT_GROUP))
        except Exception as exc:  # noqa: BLE001 - environment metadata boundary
            failures.append(
                _plugin_failure(
                    "<entry-points>",
                    "discovery",
                    "PLUGIN_DISCOVERY_FAILED",
                    exc,
                )
            )
            installed = []
        for entry_point in installed:
            self._add_candidate(
                candidates,
                failures,
                _Candidate(
                    entry_point.name,
                    _entry_point_source(entry_point, "installed"),
                    entry_point=entry_point,
                ),
            )

        for directory in self._plugin_directories:
            if not directory.is_dir():
                failures.append(
                    PluginFailure(
                        plugin=str(directory),
                        stage="discovery",
                        code="PLUGIN_DIRECTORY_INVALID",
                        message=f"Configured plugin directory does not exist: {directory}",
                    )
                )
                continue
            directory_text = str(directory)
            if directory_text not in sys.path:
                # Plain sys.path insertion deliberately avoids executable .pth files.
                sys.path.insert(0, directory_text)
            try:
                discovered_distributions = list(distributions(path=[directory_text]))
            except Exception as exc:  # noqa: BLE001
                failures.append(
                    _plugin_failure(
                        str(directory),
                        "discovery",
                        "PLUGIN_DIRECTORY_DISCOVERY_FAILED",
                        exc,
                    )
                )
                continue
            for distribution in discovered_distributions:
                for entry_point in distribution.entry_points:
                    if entry_point.group != ENTRY_POINT_GROUP:
                        continue
                    self._add_candidate(
                        candidates,
                        failures,
                        _Candidate(
                            entry_point.name,
                            _entry_point_source(entry_point, directory_text),
                            entry_point=entry_point,
                        ),
                    )
        return candidates, failures

    @staticmethod
    def _add_candidate(
        candidates: dict[str, _Candidate],
        failures: list[PluginFailure],
        candidate: _Candidate,
    ) -> None:
        previous = candidates.get(candidate.name)
        if previous is not None:
            failures.append(
                PluginFailure(
                    plugin=candidate.name,
                    stage="discovery",
                    code="PLUGIN_DISCOVERY_CONFLICT",
                    message=(
                        f"Plugin entry-point name {candidate.name!r} is provided by both "
                        f"{previous.source} and {candidate.source}."
                    ),
                )
            )
            return
        candidates[candidate.name] = candidate

    def _create_plugin(
        self,
        provider: PluginProvider,
        raw_config: dict[str, Any],
        *,
        source: str,
    ) -> tuple[Plugin, PluginMetadata]:
        if isinstance(provider, PluginRegistration):
            metadata = provider.metadata
            self._require_compatible(metadata)
            config = provider.validate_config(raw_config)
            context = PluginContext(
                logger=logging.getLogger(f"lab-platform.plugin.{metadata.name}"),
                agent_version=self._agent_version,
            )
            created_plugin = provider.create(config, context)
            return cast(Plugin, created_plugin), metadata
        if raw_config:
            raise ValueError(f"Legacy plugin from {source} does not publish a configuration schema")
        try:
            legacy_plugin = _coerce_factory(provider)()
        except Exception as exc:  # noqa: BLE001 - plugin factory boundary
            raise PluginLoadError(f"Could not initialize plugin from {source}") from exc
        metadata = _metadata(legacy_plugin)
        self._require_compatible(metadata)
        if not callable(getattr(legacy_plugin, "initialize", None)) or not callable(
            getattr(legacy_plugin, "shutdown", None)
        ):
            raise TypeError("plugin must implement initialize() and shutdown()")
        return legacy_plugin, metadata

    def _require_compatible(self, metadata: PluginMetadata) -> None:
        compatibility = self._compatibility(metadata)
        if not compatibility.compatible:
            raise PluginCompatibilityError(compatibility.message)

    def _compatibility(self, metadata: PluginMetadata) -> PluginCompatibility:
        return assess_plugin_compatibility(
            metadata,
            agent_version=self._agent_version,
            supported_plugin_api_version=self._plugin_api_version,
            platform=self._platform,
        )

    async def _initialize(self, plugin: Plugin, plugin_name: str) -> None:
        try:
            await asyncio.wait_for(
                plugin.initialize(),
                timeout=self._lifecycle_timeout_seconds,
            )
        except TimeoutError as exc:
            raise TimeoutError(
                f"Plugin {plugin_name} initialize exceeded "
                f"{self._lifecycle_timeout_seconds:g} seconds"
            ) from exc

    async def _shutdown_untracked(self, plugin: Plugin, plugin_name: str) -> None:
        try:
            await asyncio.wait_for(
                plugin.shutdown(),
                timeout=self._lifecycle_timeout_seconds,
            )
        except Exception:  # noqa: BLE001 - best-effort cleanup after failed initialize
            self._logger.warning(
                "Failed plugin cleanup after initialization error",
                extra={"plugin": plugin_name},
                exc_info=True,
            )

    async def _initial_health(
        self,
        plugin: Plugin,
        metadata: PluginMetadata,
    ) -> tuple[PluginRuntimeStatus, PluginFailure | None]:
        health_method = getattr(plugin, "health", None)
        if not callable(health_method):
            return PluginRuntimeStatus.HEALTHY, None
        try:
            health = cast(
                PluginHealth,
                await asyncio.wait_for(
                    health_method(),
                    timeout=self._lifecycle_timeout_seconds,
                ),
            )
        except Exception as exc:  # noqa: BLE001 - health boundary
            code = (
                "PLUGIN_HEALTH_TIMEOUT" if isinstance(exc, TimeoutError) else "PLUGIN_HEALTH_FAILED"
            )
            return (
                PluginRuntimeStatus.DEGRADED,
                _plugin_failure(metadata.name, "health", code, exc),
            )
        if health.status is PluginHealthStatus.HEALTHY:
            return PluginRuntimeStatus.HEALTHY, None
        return (
            PluginRuntimeStatus.DEGRADED,
            PluginFailure(
                plugin=metadata.name,
                stage="health",
                code="PLUGIN_UNHEALTHY",
                message=health.message or health.status.value,
            ),
        )

    def _load_import_path_provider(self, plugin_name: str) -> PluginProvider:
        if not self._allow_import_paths:
            raise PluginLoadError(
                "Direct import-path plugins are disabled; install an entry-point package instead"
            )
        return _load_import_path(plugin_name)

    def _load_import_path_provider_safely(
        self,
        plugin_name: str,
        failures: list[PluginFailure],
    ) -> PluginProvider | None:
        try:
            return self._load_import_path_provider(plugin_name)
        except Exception as exc:  # noqa: BLE001
            failures.append(
                _plugin_failure(
                    plugin_name,
                    "discovery",
                    "PLUGIN_DISCOVERY_FAILED",
                    exc,
                )
            )
            return None

    def _set_runtime(
        self,
        name: str,
        source: str,
        status: PluginRuntimeStatus,
        *,
        metadata: PluginMetadata | None = None,
        error_code: str | None = None,
        error_message: str | None = None,
        device_count: int = 0,
    ) -> None:
        self._runtime[name] = PluginRuntimeInfo(
            name=name,
            source=source,
            status=status,
            metadata=metadata,
            error_code=error_code,
            error_message=error_message,
            device_count=device_count,
        )

    def _failure_runtime(
        self,
        failure: PluginFailure,
        source: str,
        *,
        status: PluginRuntimeStatus = PluginRuntimeStatus.FAILED,
        metadata: PluginMetadata | None = None,
    ) -> None:
        self._set_runtime(
            failure.plugin,
            source,
            status,
            metadata=metadata,
            error_code=failure.code,
            error_message=failure.message,
        )


def _load_import_path(plugin_name: str) -> PluginProvider:
    if ":" not in plugin_name:
        raise PluginLoadError(f"Unknown plugin: {plugin_name}")
    module_name, attribute_name = plugin_name.split(":", 1)
    module = importlib.import_module(module_name)
    return cast(PluginProvider, getattr(module, attribute_name))


def _coerce_factory(loaded: Any) -> PluginFactory:
    if isinstance(loaded, PluginRegistration):
        raise TypeError("PluginRegistration must be created with a configuration and context")
    if isinstance(loaded, type):
        return cast(PluginFactory, loaded)
    if callable(loaded):
        return cast(PluginFactory, loaded)
    return lambda: cast(Plugin, loaded)


def _metadata(plugin: Any) -> PluginMetadata:
    return PluginMetadata.model_validate(plugin.metadata, from_attributes=True)


def _requested_plugins(requested: PluginConfiguration) -> list[_RequestedPlugin]:
    if isinstance(requested, Mapping):
        result: list[_RequestedPlugin] = []
        for name, value in requested.items():
            if value is None:
                result.append(_RequestedPlugin(name, True, {}))
                continue
            if isinstance(value, bool):
                result.append(_RequestedPlugin(name, value, {}))
                continue
            raw = dict(value)
            enabled = raw.pop("enabled", True)
            if not isinstance(enabled, bool):
                raise ValueError(f"plugins.{name}.enabled must be a boolean")
            nested = raw.pop("config", None)
            if nested is not None:
                if not isinstance(nested, Mapping):
                    raise ValueError(f"plugins.{name}.config must be an object")
                if raw:
                    raise ValueError(
                        f"plugins.{name} cannot mix config with top-level plugin fields: "
                        f"{', '.join(sorted(raw))}"
                    )
                config = dict(nested)
            else:
                config = raw
            result.append(_RequestedPlugin(name, enabled, config))
        return result
    return [_RequestedPlugin(name, True, {}) for name in requested]


def _plugin_failure(
    plugin: str,
    stage: Literal[
        "discovery", "configuration", "compatibility", "initialize", "health", "shutdown"
    ],
    code: str,
    error: BaseException,
) -> PluginFailure:
    return PluginFailure(
        plugin=plugin,
        stage=stage,
        code=code,
        message=str(error) or type(error).__name__,
    )


def _entry_point_source(entry_point: Any, fallback: str) -> str:
    value = getattr(entry_point, "value", None)
    return f"entry-point:{value}" if isinstance(value, str) and value else fallback


def _running_agent_version() -> str:
    from lab_platform.core.version import VERSION

    return VERSION


__all__ = ["ENTRY_POINT_GROUP", "PluginConfiguration", "PluginManager", "PluginProvider"]
