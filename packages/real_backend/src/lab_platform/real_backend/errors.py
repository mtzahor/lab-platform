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


class SerialCloseFailedError(HardwareError):
    code = "SERIAL_CLOSE_FAILED"


class SerialMessageTooLargeError(HardwareError):
    code = "SERIAL_MESSAGE_TOO_LARGE"


class SerialCaptureTooLargeError(HardwareError):
    code = "SERIAL_CAPTURE_TOO_LARGE"


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


class ExternalToolNotAvailableError(HardwareError):
    code = "EXTERNAL_TOOL_NOT_AVAILABLE"


class ExternalToolCommandFailedError(HardwareError):
    code = "EXTERNAL_TOOL_COMMAND_FAILED"


class OpenOcdNotAvailableError(ExternalToolNotAvailableError):
    code = "OPENOCD_NOT_AVAILABLE"


class OpenOcdCommandFailedError(ExternalToolCommandFailedError):
    code = "OPENOCD_COMMAND_FAILED"


class OpenOcdTimeoutError(HardwareError):
    code = "OPENOCD_TIMEOUT"


class JLinkNotAvailableError(ExternalToolNotAvailableError):
    code = "JLINK_NOT_AVAILABLE"


class JLinkCommandFailedError(ExternalToolCommandFailedError):
    code = "JLINK_COMMAND_FAILED"


class JLinkTimeoutError(HardwareError):
    code = "JLINK_TIMEOUT"


class PicotoolNotAvailableError(ExternalToolNotAvailableError):
    code = "PICOTOOL_NOT_AVAILABLE"


class PicotoolCommandFailedError(ExternalToolCommandFailedError):
    code = "PICOTOOL_COMMAND_FAILED"


class PicotoolTimeoutError(HardwareError):
    code = "PICOTOOL_TIMEOUT"


class NordicToolNotAvailableError(ExternalToolNotAvailableError):
    code = "NORDIC_TOOL_NOT_AVAILABLE"


class NordicToolCommandFailedError(ExternalToolCommandFailedError):
    code = "NORDIC_TOOL_COMMAND_FAILED"


class NordicToolTimeoutError(HardwareError):
    code = "NORDIC_TOOL_TIMEOUT"
