from __future__ import annotations

from collections.abc import Iterable

from lab_platform.plugin_sdk import (
    BaseHardwarePlugin,
    DeviceDescriptor,
    DeviceDriver,
    DeviceUnavailableError,
    DiagnosticCheck,
    DiagnosticStatus,
    PluginHealth,
    PluginHealthStatus,
    PluginMetadata,
)


class StaticHardwarePlugin(BaseHardwarePlugin):
    """Lifecycle implementation for configured official device drivers."""

    def __init__(
        self,
        metadata: PluginMetadata,
        drivers: Iterable[DeviceDriver] = (),
        *,
        dependency_checks: Iterable[DiagnosticCheck] = (),
    ) -> None:
        super().__init__(metadata)
        resolved = list(drivers)
        identifiers = [driver.descriptor.id for driver in resolved]
        if len(identifiers) != len(set(identifiers)):
            raise ValueError(f"Plugin {metadata.name} device IDs must be unique")
        self._drivers = {driver.descriptor.id: driver for driver in resolved}
        self._dependency_checks = tuple(dependency_checks)

    async def discover(self) -> list[DeviceDescriptor]:
        if not self.initialized:
            return []
        return [self._drivers[key].descriptor for key in sorted(self._drivers)]

    async def health(self) -> PluginHealth:
        if not self.initialized:
            return PluginHealth(
                status=PluginHealthStatus.UNKNOWN,
                message="Plugin is not initialized",
            )
        failures = [
            check for check in self._dependency_checks if check.status is DiagnosticStatus.FAIL
        ]
        warnings = [
            check for check in self._dependency_checks if check.status is DiagnosticStatus.WARNING
        ]
        if failures:
            return PluginHealth(
                status=PluginHealthStatus.UNHEALTHY,
                message="; ".join(check.message for check in failures),
            )
        device_health = [await driver.health() for driver in self._drivers.values()]
        degraded = warnings or any(
            health.status.value in {"degraded", "unhealthy", "offline"} for health in device_health
        )
        return PluginHealth(
            status=(PluginHealthStatus.DEGRADED if degraded else PluginHealthStatus.HEALTHY),
            message=(
                "Plugin initialized with warnings"
                if degraded
                else f"{len(self._drivers)} device(s) available"
            ),
            details={"device_count": len(self._drivers)},
        )

    async def get_driver(self, device_id: str) -> DeviceDriver:
        try:
            return self._drivers[device_id]
        except KeyError as exc:
            raise DeviceUnavailableError(
                f"Plugin {self.metadata.name} did not discover device {device_id!r}.",
                plugin=self.metadata.name,
                device_id=device_id,
            ) from exc

    async def diagnostics(self) -> list[DiagnosticCheck]:
        return list(self._dependency_checks)


__all__ = ["StaticHardwarePlugin"]
