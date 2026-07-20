from __future__ import annotations

import asyncio
import errno
import re
import time
from importlib import import_module
from typing import Protocol, cast

from lab_platform.models import SerialLine, SerialReadRequest
from lab_platform.real_backend.errors import (
    HardwareError,
    SerialDisconnectedError,
    SerialPermissionDeniedError,
    SerialPortBusyError,
    SerialReadTimeoutError,
)


class SerialHandle(Protocol):
    def readline(self) -> bytes: ...

    def close(self) -> None: ...


class SerialFactory(Protocol):
    def __call__(self, *, port: str, baudrate: int, timeout: float) -> SerialHandle: ...


def pyserial_factory(*, port: str, baudrate: int, timeout: float) -> SerialHandle:
    serial_module = import_module("serial")
    return cast(
        SerialHandle,
        serial_module.Serial(port=port, baudrate=baudrate, timeout=timeout),
    )


class Esp32SerialReader:
    def __init__(self, factory: SerialFactory = pyserial_factory) -> None:
        self._factory = factory

    async def read(
        self,
        port: str,
        baud_rate: int,
        request: SerialReadRequest,
    ) -> list[SerialLine]:
        try:
            handle = await asyncio.to_thread(
                self._factory,
                port=port,
                baudrate=baud_rate,
                timeout=min(0.25, request.timeout_seconds),
            )
        except Exception as exc:
            raise _translate_serial_error(exc, port) from exc

        lines: list[SerialLine] = []
        deadline = time.monotonic() + request.timeout_seconds
        pattern = re.compile(request.until_pattern) if request.until_pattern else None
        try:
            while time.monotonic() < deadline:
                try:
                    raw = await asyncio.to_thread(handle.readline)
                except Exception as exc:
                    translated = _translate_serial_error(exc, port)
                    if isinstance(translated, HardwareError):
                        translated.details["captured_lines"] = [
                            line.model_dump(mode="json") for line in lines
                        ]
                    raise translated from exc
                if not raw:
                    continue
                line = SerialLine(text=raw.decode("utf-8", errors="replace").rstrip("\r\n"))
                lines.append(line)
                if pattern is not None and pattern.search(line.text):
                    return lines
                if request.max_lines is not None and len(lines) >= request.max_lines:
                    if pattern is None:
                        return lines
                    break
        finally:
            await asyncio.to_thread(handle.close)

        if pattern is not None:
            raise SerialReadTimeoutError(
                f"Serial output did not match {request.until_pattern!r} before timeout.",
                serial_port=port,
                timeout_seconds=request.timeout_seconds,
                lines_read=len(lines),
                captured_lines=[line.model_dump(mode="json") for line in lines],
            )
        return lines


def _translate_serial_error(exc: Exception, port: str) -> Exception:
    error_number = getattr(exc, "errno", None)
    message = str(exc).lower()
    if isinstance(exc, PermissionError) or error_number in {errno.EACCES, errno.EPERM}:
        return SerialPermissionDeniedError(
            f"Permission denied while opening serial port {port}.", serial_port=port
        )
    if error_number in {errno.EBUSY} or "resource busy" in message or "device busy" in message:
        return SerialPortBusyError(f"Serial port {port} is busy.", serial_port=port)
    if error_number in {errno.ENODEV, errno.ENOENT, errno.EIO} or "disconnected" in message:
        return SerialDisconnectedError(f"Serial device {port} disconnected.", serial_port=port)
    return SerialDisconnectedError(
        f"Serial communication with {port} failed: {exc}", serial_port=port
    )
