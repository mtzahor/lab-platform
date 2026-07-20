from __future__ import annotations

from lab_platform.models import BenchSnapshot, BenchStatus
from lab_platform.simlab import SimulatedBenchSnapshot


def map_simlab_bench(bench: SimulatedBenchSnapshot) -> BenchSnapshot:
    capabilities = [capability.lower() for capability in bench.capabilities]
    capabilities.append("probe")
    if "power" in capabilities:
        capabilities.append("reset")
    return BenchSnapshot(
        id=bench.id,
        name=bench.name,
        status=BenchStatus.AVAILABLE if bench.online else BenchStatus.OFFLINE,
        online=bench.online,
        powered=bench.powered if bench.online else None,
        firmware_version=bench.firmware_version,
        capabilities=capabilities,
    )
