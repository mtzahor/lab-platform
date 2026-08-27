from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any, cast

from lab_platform.models import (
    FirmwareInput as BackendFirmwareInput,
)
from lab_platform.models import (
    SerialReadRequest,
    TargetHealthStatus,
)
from lab_platform.plugin_sdk import (
    Capability,
    DebugStatus,
    DeviceDescriptor,
    DeviceHealth,
    DeviceHealthStatus,
    FirmwareArtifact,
    FlashOptions,
    OperationResult,
    ProbeResult,
    ProgressUpdate,
    SerialLine,
    SerialOptions,
    UnsupportedCapabilityError,
)
from lab_platform.real_backend.targets import PhysicalTarget


class PhysicalTargetDriver:
    """Expose an existing physical target through the stable Plugin API 1.0."""

    def __init__(
        self,
        target: PhysicalTarget,
        *,
        device_type: str,
        name: str | None = None,
    ) -> None:
        self._target = target
        canonical = {
            "flash" if item.casefold() == "firmware" else item.casefold()
            for item in target.capabilities
        }
        self._descriptor = DeviceDescriptor(
            id=target.id,
            name=name or target.id,
            type=device_type,
            capabilities=canonical,
        )
        self._capabilities: dict[str, Capability] = {}
        if "probe" in canonical:
            self._capabilities["probe"] = cast(Capability, _ProbeAdapter(target))
        if "reset" in canonical:
            self._capabilities["reset"] = cast(Capability, _ResetAdapter(target))
        if "flash" in canonical:
            self._capabilities["flash"] = cast(Capability, _FlashAdapter(target))
        if "serial" in canonical:
            self._capabilities["serial"] = cast(Capability, _SerialAdapter(target))
        if "debug" in canonical:
            self._capabilities["debug"] = cast(Capability, _DebugAdapter(target))

    @property
    def descriptor(self) -> DeviceDescriptor:
        return self._descriptor

    @property
    def capabilities(self) -> set[str]:
        return set(self._capabilities)

    async def health(self) -> DeviceHealth:
        result = await self._target.probe()
        statuses = {
            TargetHealthStatus.ONLINE: DeviceHealthStatus.HEALTHY,
            TargetHealthStatus.DEGRADED: DeviceHealthStatus.DEGRADED,
            TargetHealthStatus.OFFLINE: DeviceHealthStatus.OFFLINE,
            TargetHealthStatus.UNKNOWN: DeviceHealthStatus.UNKNOWN,
        }
        return DeviceHealth(
            status=statuses[result.status],
            message=result.details.get("message", result.status.value),
            details={
                **result.details,
                "chip_type": result.chip_type,
                "serial_port": result.serial_port,
                "mac_address": result.mac_address,
            },
        )

    async def get_capability(self, name: str) -> Capability:
        normalized = "flash" if name.strip().casefold() == "firmware" else name.strip().casefold()
        try:
            return self._capabilities[normalized]
        except KeyError as exc:
            raise UnsupportedCapabilityError(
                f"Device {self._target.id!r} does not support capability {name!r}.",
                device_id=self._target.id,
                capability=normalized,
            ) from exc


class _ProbeAdapter:
    def __init__(self, target: PhysicalTarget) -> None:
        self._target = target

    async def probe(self) -> ProbeResult:
        result = await self._target.probe()
        return ProbeResult(
            online=result.status is TargetHealthStatus.ONLINE,
            device_type=result.chip_type,
            serial_number=result.mac_address,
            details={**result.details, "serial_port": result.serial_port},
        )


class _ResetAdapter:
    def __init__(self, target: PhysicalTarget) -> None:
        self._target = target

    async def reset(self) -> OperationResult:
        await self._target.reset()
        return OperationResult(message="Target reset completed")


class _FlashAdapter:
    def __init__(self, target: PhysicalTarget) -> None:
        self._target = target

    async def flash(
        self,
        image: FirmwareArtifact,
        options: FlashOptions,
    ) -> AsyncIterator[ProgressUpdate]:
        defaults = FlashOptions()
        unsupported_options: dict[str, Any] = {
            name: getattr(options, name)
            for name in ("verify", "reset_after", "timeout_seconds")
            if getattr(options, name) != getattr(defaults, name)
        }
        if options.values:
            unsupported_options["values"] = options.values
        if unsupported_options:
            names = ", ".join(sorted(unsupported_options))
            raise UnsupportedCapabilityError(
                f"Physical target {self._target.id!r} does not support per-operation "
                f"flash option overrides: {names}.",
                device_id=self._target.id,
                capability="flash",
                unsupported_options=unsupported_options,
            )
        backend_image = BackendFirmwareInput(
            filename=image.filename,
            local_path=image.local_path,
            sha256=image.sha256,
            size_bytes=image.size_bytes,
            version=image.version,
        )
        async for update in self._target.flash(backend_image):
            yield ProgressUpdate(
                percent=update.percent,
                message=update.message,
                details={
                    "firmware_version": update.firmware_version,
                    "serial_lines": [line.model_dump(mode="json") for line in update.serial_lines],
                    "verify_requested": options.verify,
                    "reset_after_requested": options.reset_after,
                },
            )


class _SerialAdapter:
    def __init__(self, target: PhysicalTarget) -> None:
        self._target = target

    async def stream(self, options: SerialOptions) -> AsyncIterator[SerialLine]:
        if options.baud_rate is not None:
            raise UnsupportedCapabilityError(
                f"Physical target {self._target.id!r} does not support a per-stream "
                "baud-rate override; configure the target connection baud rate instead.",
                device_id=self._target.id,
                capability="serial",
                unsupported_options={"baud_rate": options.baud_rate},
            )
        request = SerialReadRequest(
            timeout_seconds=options.timeout_seconds,
            until_pattern=options.until_pattern,
            max_lines=options.max_lines,
        )
        async for line in self._target.read_serial(request):
            yield SerialLine(
                timestamp=line.timestamp,
                text=line.text,
                stream=line.stream,
            )


class _DebugAdapter:
    def __init__(self, target: PhysicalTarget) -> None:
        self._target = target

    async def status(self) -> DebugStatus:
        callback = getattr(self._target, "debug_status", None)
        if callback is None:
            return DebugStatus(available=True, message="Debug capability is configured")
        raw = await callback()
        if isinstance(raw, DebugStatus):
            return raw
        if not isinstance(raw, dict):
            return DebugStatus(available=True, message=str(raw))
        return DebugStatus(
            available=str(raw.get("status", "available")).casefold()
            not in {"unavailable", "failed"},
            connected=bool(raw.get("connected", False)),
            endpoint=(str(raw["endpoint"]) if raw.get("endpoint") is not None else None),
            message=str(raw.get("detail", raw.get("message", ""))),
        )


__all__ = ["PhysicalTargetDriver"]
