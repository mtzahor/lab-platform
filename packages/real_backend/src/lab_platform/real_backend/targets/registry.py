from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass

from lab_platform.config import HardwareBenchSettings
from lab_platform.core.errors import ConfigurationError
from lab_platform.real_backend.discovery import SerialPortDiscovery
from lab_platform.real_backend.process_runner import ProcessRunner
from lab_platform.real_backend.targets.base import PhysicalTarget
from lab_platform.real_backend.targets.esp32 import Esp32Target
from lab_platform.real_backend.targets.esp32.serial import Esp32SerialReader
from lab_platform.real_backend.targets.jlink import JLinkTarget
from lab_platform.real_backend.targets.nrf52 import Nrf52Target
from lab_platform.real_backend.targets.openocd import OpenOcdTarget
from lab_platform.real_backend.targets.rp2040 import Rp2040Target


@dataclass(frozen=True, slots=True)
class TargetDependencies:
    discovery: SerialPortDiscovery
    process_runner: ProcessRunner
    serial_reader: Esp32SerialReader


TargetFactory = Callable[[HardwareBenchSettings, TargetDependencies], PhysicalTarget]


class TargetDriverRegistry:
    """Maps stable target type names to vendor-specific driver factories."""

    def __init__(self, factories: Mapping[str, TargetFactory] | None = None) -> None:
        self._factories: dict[str, TargetFactory] = {}
        for name, factory in (factories or {}).items():
            self.register(name, factory)

    def register(self, target_type: str, factory: TargetFactory) -> None:
        normalized = _normalize_target_type(target_type)
        existing = self._factories.get(normalized)
        if existing is not None and existing is not factory:
            raise ConfigurationError(
                f"Target driver {normalized!r} is already registered.",
                target_type=normalized,
            )
        self._factories[normalized] = factory

    def create(
        self,
        config: HardwareBenchSettings,
        dependencies: TargetDependencies,
    ) -> PhysicalTarget:
        normalized = _normalize_target_type(config.target_type)
        factory = self._factories.get(normalized)
        if factory is None:
            raise ConfigurationError(
                f"Unsupported physical target type: {config.target_type}",
                bench_id=config.id,
                target_type=config.target_type,
                available_target_types=sorted(self._factories),
            )
        return factory(config, dependencies)

    @property
    def target_types(self) -> tuple[str, ...]:
        return tuple(sorted(self._factories))


def default_target_registry() -> TargetDriverRegistry:
    registry = TargetDriverRegistry()

    def create_esp32(
        config: HardwareBenchSettings,
        dependencies: TargetDependencies,
    ) -> PhysicalTarget:
        return Esp32Target(
            config,
            dependencies.discovery,
            dependencies.process_runner,
            dependencies.serial_reader,
        )

    def create_openocd(
        config: HardwareBenchSettings,
        dependencies: TargetDependencies,
    ) -> PhysicalTarget:
        return OpenOcdTarget(
            config,
            dependencies.discovery,
            dependencies.process_runner,
            dependencies.serial_reader,
        )

    def create_rp2040(
        config: HardwareBenchSettings,
        dependencies: TargetDependencies,
    ) -> PhysicalTarget:
        return Rp2040Target(
            config,
            dependencies.discovery,
            dependencies.process_runner,
            dependencies.serial_reader,
        )

    def create_jlink(
        config: HardwareBenchSettings,
        dependencies: TargetDependencies,
    ) -> PhysicalTarget:
        return JLinkTarget(
            config,
            dependencies.discovery,
            dependencies.process_runner,
            dependencies.serial_reader,
        )

    def create_nrf52(
        config: HardwareBenchSettings,
        dependencies: TargetDependencies,
    ) -> PhysicalTarget:
        if config.nrf52.tool == "jlink":
            return create_jlink(config, dependencies)
        return Nrf52Target(
            config,
            dependencies.discovery,
            dependencies.process_runner,
            dependencies.serial_reader,
        )

    registry.register("esp32", create_esp32)
    registry.register("openocd", create_openocd)
    registry.register("stm32", create_openocd)
    registry.register("rp2040", create_rp2040)
    registry.register("pico", create_rp2040)
    registry.register("raspberry-pi-pico", create_rp2040)
    registry.register("jlink", create_jlink)
    registry.register("nrf52", create_nrf52)
    return registry


def _normalize_target_type(value: str) -> str:
    normalized = value.strip().casefold().replace("_", "-")
    if not normalized:
        raise ConfigurationError("Physical target type cannot be empty.")
    return normalized


__all__ = [
    "TargetDependencies",
    "TargetDriverRegistry",
    "TargetFactory",
    "default_target_registry",
]
