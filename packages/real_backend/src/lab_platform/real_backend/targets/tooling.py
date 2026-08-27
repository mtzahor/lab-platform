from __future__ import annotations

import asyncio
import hashlib

from lab_platform.models import FirmwareInput
from lab_platform.real_backend.errors import FirmwareVerificationFailedError
from lab_platform.real_backend.process_runner import ProcessResult


async def verify_firmware(firmware: FirmwareInput) -> None:
    """Revalidate an uploaded artifact immediately before an external tool sees it."""

    if not firmware.local_path.is_file():
        raise FirmwareVerificationFailedError(
            f"Firmware file {firmware.filename} no longer exists.",
            filename=firmware.filename,
        )

    def calculate() -> tuple[str, int]:
        digest = hashlib.sha256()
        size = 0
        with firmware.local_path.open("rb") as stream:
            while chunk := stream.read(1024 * 1024):
                digest.update(chunk)
                size += len(chunk)
        return digest.hexdigest(), size

    checksum, size = await asyncio.to_thread(calculate)
    if checksum != firmware.sha256 or size != firmware.size_bytes:
        raise FirmwareVerificationFailedError(
            "Firmware checksum or size changed after upload.",
            expected_sha256=firmware.sha256,
            actual_sha256=checksum,
            expected_size=firmware.size_bytes,
            actual_size=size,
        )


def last_output(result: ProcessResult, fallback: str) -> str:
    return next(
        (line for line in reversed((*result.stderr, *result.stdout)) if line.strip()),
        fallback,
    )


def safe_tool_path(path: str, *, tool: str) -> str:
    """Reject characters which acquire meaning in debugger command languages."""

    if any(character in path for character in ("\x00", "\n", "\r", ";", "{", "}", '"')):
        raise FirmwareVerificationFailedError(
            f"Firmware path contains characters unsafe for {tool}.",
            path=path,
        )
    return path


__all__ = ["last_output", "safe_tool_path", "verify_firmware"]
