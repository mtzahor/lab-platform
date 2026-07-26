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
    SerialCaptureTooLargeError,
    SerialCloseFailedError,
    SerialDisconnectedError,
    SerialMessageTooLargeError,
    SerialPermissionDeniedError,
    SerialPortBusyError,
    SerialReadTimeoutError,
)


class SerialHandle(Protocol):
    def readline(self, size: int = -1) -> bytes: ...

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
    def __init__(
        self,
        factory: SerialFactory = pyserial_factory,
        *,
        decode_errors: str = "replace",
        maximum_capture_bytes: int = 50 * 1024 * 1024,
        maximum_message_bytes: int = 1024 * 1024,
    ) -> None:
        if decode_errors not in {"replace", "strict", "ignore"}:
            raise ValueError("unsupported serial decode error policy")
        if maximum_capture_bytes <= 0:
            raise ValueError("serial capture size limit must be positive")
        if maximum_message_bytes <= 0:
            raise ValueError("serial message size limit must be positive")
        self._factory = factory
        self._decode_errors = decode_errors
        self._maximum_capture_bytes = maximum_capture_bytes
        self._maximum_message_bytes = min(maximum_message_bytes, maximum_capture_bytes)

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
        captured_bytes = 0
        deadline = time.monotonic() + request.timeout_seconds
        pattern = re.compile(request.until_pattern) if request.until_pattern else None
        try:
            while time.monotonic() < deadline:
                try:
                    raw = await asyncio.to_thread(
                        handle.readline,
                        self._maximum_message_bytes + 1,
                    )
                except Exception as exc:
                    translated = _translate_serial_error(exc, port)
                    if isinstance(translated, HardwareError):
                        translated.details["captured_lines"] = [
                            line.model_dump(mode="json") for line in lines
                        ]
                    raise translated from exc
                if not raw:
                    continue
                if len(raw) > self._maximum_message_bytes:
                    raise SerialMessageTooLargeError(
                        "Serial message exceeds the configured size limit.",
                        serial_port=port,
                        maximum_message_size_bytes=self._maximum_message_bytes,
                        captured_lines=[line.model_dump(mode="json") for line in lines],
                    )
                line = SerialLine(
                    text=raw.decode("utf-8", errors=self._decode_errors).rstrip("\r\n")
                )
                line_size = len(line.text.encode("utf-8", errors="replace")) + 1
                if captured_bytes + line_size > self._maximum_capture_bytes:
                    raise SerialCaptureTooLargeError(
                        "Serial capture exceeds the configured size limit.",
                        serial_port=port,
                        maximum_capture_size_bytes=self._maximum_capture_bytes,
                        captured_lines=[line.model_dump(mode="json") for line in lines],
                    )
                lines.append(line)
                captured_bytes += line_size
                if pattern is not None and pattern.search(line.text):
                    return lines
                if request.max_lines is not None and len(lines) >= request.max_lines:
                    if pattern is None:
                        return lines
                    break
        finally:
            try:
                await asyncio.to_thread(handle.close)
            except Exception as exc:
                raise SerialCloseFailedError(
                    f"Could not close serial port {port}.",
                    serial_port=port,
                    captured_lines=[line.model_dump(mode="json") for line in lines],
                ) from exc

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
