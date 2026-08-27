from __future__ import annotations

import asyncio
import json
import socket
import struct
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol, cast
from uuid import uuid4

from lab_platform.plugin_sdk import (
    ArtifactReference,
    ArtifactSink,
    CanFilter,
    CanFrame,
    Capability,
    CaptureRequest,
    DeviceDescriptor,
    DeviceHealth,
    DeviceHealthStatus,
    PluginMetadata,
    PluginOperationError,
    UnsupportedCapabilityError,
)
from lab_platform.plugins.hardware_base import StaticHardwarePlugin

_CAN_FRAME = struct.Struct("=IB3x8s")
_CAN_EFF_FLAG = 0x80000000
_CAN_RTR_FLAG = 0x40000000
_CAN_EFF_MASK = 0x1FFFFFFF
_CAN_SFF_MASK = 0x7FF


class CanTransport(Protocol):
    async def health(self) -> tuple[bool, str]: ...

    async def send(self, frame: CanFrame) -> None: ...

    def receive(self, filter: CanFilter) -> AsyncIterator[CanFrame]: ...


def frame_matches(frame: CanFrame, filter: CanFilter) -> bool:
    if filter.extended is not None and frame.extended is not filter.extended:
        return False
    if filter.arbitration_id is None:
        return True
    mask = filter.mask if filter.mask is not None else _CAN_EFF_MASK
    return frame.arbitration_id & mask == filter.arbitration_id & mask


class MemoryCanTransport:
    """Deterministic SocketCAN-compatible transport for contracts and SimLab."""

    def __init__(self, frames: list[CanFrame] | None = None) -> None:
        self.frames = list(frames or [])
        self.sent: list[CanFrame] = []

    async def health(self) -> tuple[bool, str]:
        return True, "Memory CAN transport is available"

    async def send(self, frame: CanFrame) -> None:
        self.sent.append(frame)

    async def receive(self, filter: CanFilter) -> AsyncIterator[CanFrame]:
        for frame in self.frames:
            if frame_matches(frame, filter):
                yield frame


class SocketCanTransport:
    """Linux SocketCAN transport using kernel CAN sockets and no shell commands."""

    def __init__(self, interface: str) -> None:
        safe = interface and all(
            character.isalnum() or character in "._-" for character in interface
        )
        if not safe:
            raise ValueError("SocketCAN interface contains unsupported characters")
        self.interface = interface

    async def health(self) -> tuple[bool, str]:
        if not hasattr(socket, "AF_CAN"):
            return False, "SocketCAN is unavailable on this operating system"
        if not Path("/sys/class/net", self.interface).exists():
            return False, f"SocketCAN interface {self.interface} was not found"
        return True, f"SocketCAN interface {self.interface} is available"

    async def send(self, frame: CanFrame) -> None:
        if len(frame.data) > 8:
            raise PluginOperationError(
                "Classic SocketCAN transport supports payloads up to 8 bytes.",
                interface=self.interface,
            )
        raw_identifier = frame.arbitration_id
        if frame.extended:
            raw_identifier |= _CAN_EFF_FLAG
        if frame.remote:
            raw_identifier |= _CAN_RTR_FLAG
        payload = _CAN_FRAME.pack(raw_identifier, len(frame.data), frame.data.ljust(8, b"\0"))

        def transmit() -> None:
            with self._open_socket() as can_socket:
                can_socket.send(payload)

        try:
            await asyncio.to_thread(transmit)
        except OSError as exc:
            raise PluginOperationError(
                f"SocketCAN transmit failed on {self.interface}: {exc}",
                interface=self.interface,
            ) from exc

    async def receive(self, filter: CanFilter) -> AsyncIterator[CanFrame]:
        try:
            can_socket = self._open_socket()
        except OSError as exc:
            raise PluginOperationError(
                f"SocketCAN receive failed on {self.interface}: {exc}",
                interface=self.interface,
            ) from exc
        can_socket.settimeout(0.25)
        try:
            while True:
                try:
                    payload = await asyncio.to_thread(can_socket.recv, _CAN_FRAME.size)
                except TimeoutError:
                    await asyncio.sleep(0)
                    continue
                identifier, length, data = _CAN_FRAME.unpack(payload)
                extended = bool(identifier & _CAN_EFF_FLAG)
                frame = CanFrame(
                    arbitration_id=identifier & (_CAN_EFF_MASK if extended else _CAN_SFF_MASK),
                    data=data[:length],
                    extended=extended,
                    remote=bool(identifier & _CAN_RTR_FLAG),
                    timestamp=datetime.now(UTC),
                )
                if frame_matches(frame, filter):
                    yield frame
        except OSError as exc:
            raise PluginOperationError(
                f"SocketCAN receive failed on {self.interface}: {exc}",
                interface=self.interface,
            ) from exc
        finally:
            can_socket.close()

    def _open_socket(self) -> socket.socket:
        if not hasattr(socket, "AF_CAN") or not hasattr(socket, "CAN_RAW"):
            raise OSError("SocketCAN is unavailable")
        can_socket = socket.socket(
            socket.AF_CAN,
            socket.SOCK_RAW,
            socket.CAN_RAW,  # type: ignore[attr-defined]
        )
        try:
            can_socket.bind((self.interface,))
        except BaseException:
            can_socket.close()
            raise
        return can_socket


class SocketCanCapability:
    def __init__(self, transport: CanTransport) -> None:
        self._transport = transport

    async def send(self, frame: CanFrame) -> None:
        await self._transport.send(frame)

    async def receive(self, filter: CanFilter) -> AsyncIterator[CanFrame]:
        async for frame in self._transport.receive(filter):
            yield frame


class CanCaptureCapability:
    def __init__(self, transport: CanTransport, artifact_sink: ArtifactSink | None = None) -> None:
        self._transport = transport
        self._artifact_sink = artifact_sink

    async def capture(self, request: CaptureRequest) -> ArtifactReference:
        frames: list[CanFrame] = []
        can_filter = _capture_filter(request)
        try:
            async with asyncio.timeout(request.duration_seconds):
                async for frame in self._transport.receive(can_filter):
                    frames.append(frame)
        except TimeoutError:
            pass
        serializable = [
            {
                "arbitration_id": frame.arbitration_id,
                "data_hex": frame.data.hex(),
                "extended": frame.extended,
                "remote": frame.remote,
                "timestamp": frame.timestamp.isoformat() if frame.timestamp else None,
            }
            for frame in frames
        ]
        content = json.dumps(serializable, separators=(",", ":")).encode("utf-8")
        if self._artifact_sink is not None:
            return await self._artifact_sink.store(
                name=f"can-{uuid4()}.json",
                media_type="application/json",
                content=content,
                metadata={"frame_count": len(frames)},
            )
        return ArtifactReference(
            id=f"inline-can-capture-{uuid4()}",
            media_type="application/json",
            metadata={"frame_count": len(frames), "content": content.decode("utf-8")},
        )


def _capture_filter(request: CaptureRequest) -> CanFilter:
    if request.trigger is None or request.trigger.type.casefold() != "can_id":
        return CanFilter()
    value = request.trigger.value
    if isinstance(value, str):
        arbitration_id = int(value, 0)
    elif isinstance(value, (int, float)):
        arbitration_id = int(value)
    else:
        return CanFilter()
    return CanFilter(arbitration_id=arbitration_id)


class SocketCanDriver:
    def __init__(
        self,
        *,
        interface: str,
        transport: CanTransport | None = None,
        artifact_sink: ArtifactSink | None = None,
    ) -> None:
        self._transport = transport or SocketCanTransport(interface)
        self._descriptor = DeviceDescriptor(
            id=f"socketcan:{interface}",
            name=f"SocketCAN {interface}",
            type="socketcan",
            capabilities={"can", "capture"},
            metadata={"interface": interface},
        )
        self._can = SocketCanCapability(self._transport)
        self._capture = CanCaptureCapability(self._transport, artifact_sink)

    @property
    def descriptor(self) -> DeviceDescriptor:
        return self._descriptor

    @property
    def capabilities(self) -> set[str]:
        return {"can", "capture"}

    async def health(self) -> DeviceHealth:
        healthy, message = await self._transport.health()
        return DeviceHealth(
            status=DeviceHealthStatus.HEALTHY if healthy else DeviceHealthStatus.UNHEALTHY,
            message=message,
        )

    async def get_capability(self, name: str) -> Capability:
        normalized = name.strip().casefold()
        if normalized == "can":
            return cast(Capability, self._can)
        if normalized == "capture":
            return cast(Capability, self._capture)
        raise UnsupportedCapabilityError(
            f"SocketCAN does not support capability {name!r}.",
            device_id=self._descriptor.id,
            capability=normalized,
        )


class SocketCanPlugin(StaticHardwarePlugin):
    metadata_definition = PluginMetadata(
        name="socketcan",
        version="1.0.0",
        plugin_api_version="1.0",
        vendor="Lab Platform",
        author="Lab Platform Contributors",
        description="Classic CAN transmit, receive, filtering, and capture through SocketCAN.",
        supported_platforms=["linux"],
        supported_devices=["Linux SocketCAN interfaces", "USB CAN adapters exposed by SocketCAN"],
        capabilities=["can", "capture"],
        minimum_agent_version="0.9.0-beta",
    )

    def __init__(
        self,
        interfaces: list[str] | None = None,
        *,
        transports: dict[str, CanTransport] | None = None,
        artifact_sink: ArtifactSink | None = None,
    ) -> None:
        configured = sorted(set(interfaces or (transports or {}).keys()))
        drivers = [
            SocketCanDriver(
                interface=interface,
                transport=(transports or {}).get(interface),
                artifact_sink=artifact_sink,
            )
            for interface in configured
        ]
        super().__init__(self.metadata_definition, drivers)


__all__ = [
    "CanCaptureCapability",
    "CanTransport",
    "MemoryCanTransport",
    "SocketCanCapability",
    "SocketCanDriver",
    "SocketCanPlugin",
    "SocketCanTransport",
    "frame_matches",
]
