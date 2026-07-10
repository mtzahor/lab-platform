from __future__ import annotations

from lab_platform.models import Bench, BenchStatus, Device

_CAPABILITY_PATTERNS = (
    ("Power", "Serial", "Firmware"),
    ("Power", "Serial"),
    ("Power", "Serial", "Debugger"),
    ("Power",),
    ("Power", "Serial"),
)


class SimLab:
    def __init__(self, enabled: bool = True, bench_count: int = 5) -> None:
        self._enabled = enabled
        self._bench_count = bench_count
        self._started = False

    async def start(self) -> None:
        self._started = True

    async def shutdown(self) -> None:
        self._started = False

    @property
    def started(self) -> bool:
        return self._started

    def benches(self) -> list[Bench]:
        if not self._enabled or not self._started:
            return []
        width = max(2, len(str(self._bench_count)))
        benches: list[Bench] = []
        for index in range(self._bench_count):
            name = f"bench-{index + 1:0{width}d}"
            capabilities = list(_CAPABILITY_PATTERNS[index % len(_CAPABILITY_PATTERNS)])
            benches.append(
                Bench(
                    name=name,
                    status=BenchStatus.ONLINE,
                    capabilities=capabilities,
                    devices=[
                        Device(name=f"{name}-controller", kind="controller"),
                    ],
                )
            )
        return benches
