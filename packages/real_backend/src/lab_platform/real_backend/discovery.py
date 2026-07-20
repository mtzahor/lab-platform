from __future__ import annotations

from importlib import import_module
from pathlib import Path
from typing import Protocol

from lab_platform.config import SerialConnectionSettings
from lab_platform.models import SerialPortInfo
from lab_platform.real_backend.errors import (
    DeviceNotFoundError,
    SerialPortAmbiguousError,
    SerialPortNotFoundError,
)


class SerialPortProvider(Protocol):
    def list_ports(self) -> list[SerialPortInfo]: ...


class PySerialPortProvider:
    def list_ports(self) -> list[SerialPortInfo]:
        list_ports = import_module("serial.tools.list_ports")
        return [
            SerialPortInfo(
                device=str(port.device),
                description=_optional_string(port.description),
                manufacturer=_optional_string(port.manufacturer),
                serial_number=_optional_string(port.serial_number),
                vendor_id=port.vid,
                product_id=port.pid,
            )
            for port in list_ports.comports()
        ]


class SerialPortDiscovery:
    def __init__(self, provider: SerialPortProvider | None = None) -> None:
        self._provider = provider or PySerialPortProvider()

    def resolve(self, connection: SerialConnectionSettings) -> SerialPortInfo:
        ports = self._provider.list_ports()
        configured = connection.serial_port
        if configured.lower() != "auto":
            for port in ports:
                if port.device == configured:
                    return port
            if Path(configured).exists():
                return SerialPortInfo(device=configured)
            raise SerialPortNotFoundError(
                f"Configured serial port {configured} does not exist.",
                serial_port=configured,
            )

        usb = connection.usb
        candidates = ports
        if usb.serial_number is not None:
            serial_matches = [
                port for port in candidates if port.serial_number == usb.serial_number
            ]
            if len(serial_matches) == 1:
                return serial_matches[0]
            candidates = serial_matches

        if usb.vendor_id is not None:
            candidates = [port for port in candidates if port.vendor_id == usb.vendor_id]
        if usb.product_id is not None:
            candidates = [port for port in candidates if port.product_id == usb.product_id]

        if len(candidates) == 1:
            return candidates[0]
        if len(candidates) > 1:
            raise SerialPortAmbiguousError(
                "Multiple serial ports match the configured ESP32 bench.",
                candidates=[port.device for port in candidates],
            )
        raise DeviceNotFoundError(
            "No serial device matches the configured ESP32 bench.",
            serial_number=usb.serial_number,
            vendor_id=usb.vendor_id,
            product_id=usb.product_id,
        )


def _optional_string(value: object) -> str | None:
    return str(value) if value is not None else None
