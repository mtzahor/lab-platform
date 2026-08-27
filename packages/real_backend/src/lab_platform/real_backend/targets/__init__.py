from lab_platform.real_backend.targets.base import PhysicalTarget
from lab_platform.real_backend.targets.jlink import JLinkTarget
from lab_platform.real_backend.targets.nrf52 import Nrf52Target
from lab_platform.real_backend.targets.openocd import OpenOcdTarget
from lab_platform.real_backend.targets.registry import (
    TargetDependencies,
    TargetDriverRegistry,
    TargetFactory,
    default_target_registry,
)
from lab_platform.real_backend.targets.rp2040 import Rp2040Target

__all__ = [
    "JLinkTarget",
    "Nrf52Target",
    "OpenOcdTarget",
    "PhysicalTarget",
    "Rp2040Target",
    "TargetDependencies",
    "TargetDriverRegistry",
    "TargetFactory",
    "default_target_registry",
]
