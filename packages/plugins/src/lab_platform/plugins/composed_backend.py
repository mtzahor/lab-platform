from __future__ import annotations

from collections.abc import AsyncIterator, Mapping
from typing import cast

from lab_platform.core.resources import ResourceCatalog, ResourceUnavailableError
from lab_platform.models import (
    BackendProgress,
    BenchSnapshot,
    BenchStatus,
    FirmwareInput,
    SerialLine,
    SerialReadRequest,
    TargetHealth,
    TargetHealthStatus,
)
from lab_platform.plugin_sdk import (
    DeviceDriver,
    DeviceHealthStatus,
    FirmwareArtifact,
    FlashCapability,
    FlashOptions,
    PowerCapability,
    ProbeCapability,
    ResetCapability,
    SerialCapability,
    SerialOptions,
)


class ComposedHardwareBackend:
    """Expose multiple Plugin API resources as one ordinary ``LabBackend`` bench."""

    def __init__(
        self,
        catalog: ResourceCatalog,
        drivers: Mapping[str, DeviceDriver],
    ) -> None:
        self._catalog = catalog
        self._drivers = dict(drivers)
        self._started = False
        self._powered: dict[str, bool] = {}
        self._firmware_versions: dict[str, str | None] = {}

    async def start(self) -> None:
        for bench in self._catalog.list_benches():
            for binding in bench.resources:
                if binding.required and binding.resource_id not in self._drivers:
                    raise ResourceUnavailableError(
                        f"No Plugin API driver is available for {binding.resource_id!r}.",
                        bench_id=bench.id,
                        resource_id=binding.resource_id,
                    )
        self._started = True

    async def stop(self) -> None:
        self._started = False

    async def list_benches(self) -> list[BenchSnapshot]:
        return [await self.get_bench(bench.id) for bench in self._catalog.list_benches()]

    async def get_bench(self, bench_id: str) -> BenchSnapshot:
        self._require_started()
        composition = self._catalog.get_bench(bench_id)
        self._catalog.require_available(bench_id)
        health = [
            await self._drivers[item.resource_id].health()
            for item in composition.resources
            if item.required
        ]
        online = all(
            item.status not in {DeviceHealthStatus.OFFLINE, DeviceHealthStatus.UNHEALTHY}
            for item in health
        )
        return BenchSnapshot(
            id=composition.id,
            name=composition.name,
            status=BenchStatus.AVAILABLE if online else BenchStatus.OFFLINE,
            online=online,
            powered=self._powered.get(bench_id),
            firmware_version=self._firmware_versions.get(bench_id),
            capabilities=sorted(self._catalog.capabilities_for_bench(bench_id)),
        )

    async def power_on(self, bench_id: str) -> None:
        capability = cast(PowerCapability, await self._capability(bench_id, "power"))
        await capability.on()
        self._powered[bench_id] = True

    async def power_off(self, bench_id: str) -> None:
        capability = cast(PowerCapability, await self._capability(bench_id, "power"))
        await capability.off()
        self._powered[bench_id] = False

    async def power_cycle(self, bench_id: str) -> None:
        capability = cast(PowerCapability, await self._capability(bench_id, "power"))
        await capability.cycle()
        self._powered[bench_id] = True

    async def reset(self, bench_id: str) -> None:
        capability = cast(ResetCapability, await self._capability(bench_id, "reset"))
        await capability.reset()

    async def probe(self, bench_id: str) -> TargetHealth:
        capability = cast(ProbeCapability, await self._capability(bench_id, "probe"))
        result = await capability.probe()
        return TargetHealth(
            bench_id=bench_id,
            status=TargetHealthStatus.ONLINE if result.online else TargetHealthStatus.OFFLINE,
            chip_type=result.device_type,
            mac_address=result.serial_number,
            details={key: str(value) for key, value in result.details.items()},
        )

    async def flash_firmware(
        self,
        bench_id: str,
        firmware: FirmwareInput,
    ) -> AsyncIterator[BackendProgress]:
        capability = cast(FlashCapability, await self._capability(bench_id, "flash"))
        artifact = FirmwareArtifact(
            filename=firmware.filename,
            local_path=firmware.local_path,
            sha256=firmware.sha256,
            size_bytes=firmware.size_bytes,
            version=firmware.version,
        )
        async for update in capability.flash(artifact, FlashOptions()):
            raw_lines = update.details.get("serial_lines", [])
            lines = [SerialLine.model_validate(line) for line in raw_lines]
            if update.percent == 100:
                self._firmware_versions[bench_id] = firmware.version
            yield BackendProgress(
                percent=update.percent,
                message=update.message,
                serial_lines=lines,
                firmware_version=(firmware.version if update.percent == 100 else None),
            )

    async def read_serial(
        self,
        bench_id: str,
        request: SerialReadRequest,
    ) -> AsyncIterator[SerialLine]:
        capability = cast(SerialCapability, await self._capability(bench_id, "serial"))
        options = SerialOptions(
            timeout_seconds=request.timeout_seconds,
            until_pattern=request.until_pattern,
            max_lines=request.max_lines,
        )
        async for line in capability.stream(options):
            yield SerialLine(
                timestamp=line.timestamp,
                text=line.text,
                stream=line.stream,
            )

    async def _capability(self, bench_id: str, name: str) -> object:
        self._require_started()
        self._catalog.require_available(bench_id)
        composition = self._catalog.get_bench(bench_id)
        for binding in composition.resources:
            resource = self._catalog.get_resource(binding.resource_id)
            exposed = binding.capabilities or resource.capabilities
            if name not in exposed:
                continue
            driver = self._drivers.get(binding.resource_id)
            if driver is None:
                continue
            return await driver.get_capability(name)
        raise ResourceUnavailableError(
            f"Composed bench {bench_id!r} does not provide capability {name!r}.",
            bench_id=bench_id,
            capability=name,
        )

    def _require_started(self) -> None:
        if not self._started:
            raise RuntimeError("Composed hardware backend is not started")


__all__ = ["ComposedHardwareBackend"]
