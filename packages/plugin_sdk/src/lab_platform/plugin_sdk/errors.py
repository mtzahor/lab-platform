from __future__ import annotations

from typing import Any


class PluginSdkError(RuntimeError):
    code = "PLUGIN_ERROR"

    def __init__(self, message: str, **details: Any) -> None:
        super().__init__(message)
        self.message = message
        self.details = details


class PluginConfigurationError(PluginSdkError):
    code = "PLUGIN_CONFIGURATION_INVALID"


class PluginCompatibilityError(PluginSdkError):
    code = "PLUGIN_INCOMPATIBLE"


class PluginDependencyError(PluginSdkError):
    code = "PLUGIN_DEPENDENCY_UNAVAILABLE"


class PluginTimeoutError(PluginSdkError):
    code = "PLUGIN_TIMEOUT"


class PluginOperationError(PluginSdkError):
    code = "PLUGIN_OPERATION_FAILED"


class DeviceUnavailableError(PluginOperationError):
    code = "DEVICE_UNAVAILABLE"


class UnsupportedCapabilityError(PluginOperationError):
    code = "CAPABILITY_NOT_SUPPORTED"


__all__ = [
    "DeviceUnavailableError",
    "PluginCompatibilityError",
    "PluginConfigurationError",
    "PluginDependencyError",
    "PluginOperationError",
    "PluginSdkError",
    "PluginTimeoutError",
    "UnsupportedCapabilityError",
]
