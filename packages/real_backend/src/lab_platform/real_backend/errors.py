from __future__ import annotations

from lab_platform.core.errors import PlatformError


class HardwareError(PlatformError):
    """Base class for stable physical-hardware failures."""


class DeviceNotFoundError(HardwareError):
    code = "DEVICE_NOT_FOUND"


class SerialPortNotFoundError(HardwareError):
    code = "SERIAL_PORT_NOT_FOUND"


class SerialPortAmbiguousError(HardwareError):
    code = "SERIAL_PORT_AMBIGUOUS"


class SerialPortBusyError(HardwareError):
    code = "SERIAL_PORT_BUSY"


class SerialPermissionDeniedError(HardwareError):
    code = "SERIAL_PERMISSION_DENIED"


class SerialReadTimeoutError(HardwareError):
    code = "SERIAL_READ_TIMEOUT"


class SerialDisconnectedError(HardwareError):
    code = "SERIAL_DISCONNECTED"


class EsptoolNotAvailableError(HardwareError):
    code = "ESPTOOL_NOT_AVAILABLE"


class EsptoolConnectionFailedError(HardwareError):
    code = "ESPTOOL_CONNECTION_FAILED"


class EsptoolFlashFailedError(HardwareError):
    code = "ESPTOOL_FLASH_FAILED"


class EsptoolTimeoutError(HardwareError):
    code = "ESPTOOL_TIMEOUT"


class WrongTargetTypeError(HardwareError):
    code = "WRONG_TARGET_TYPE"


class BootVerificationFailedError(HardwareError):
    code = "BOOT_VERIFICATION_FAILED"


class BootTimeoutError(HardwareError):
    code = "BOOT_TIMEOUT"


class FirmwareVerificationFailedError(HardwareError):
    code = "FIRMWARE_VERIFICATION_FAILED"


class ProcessCancelledError(HardwareError):
    code = "PROCESS_CANCELLED"


class ProcessTimeoutError(HardwareError):
    code = "PROCESS_TIMEOUT"


class ProcessExecutableNotFoundError(RuntimeError):
    pass


class ProcessExecutionTimeoutError(RuntimeError):
    pass
