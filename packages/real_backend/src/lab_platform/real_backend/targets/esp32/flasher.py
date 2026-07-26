from __future__ import annotations

import asyncio
import sys
from collections.abc import AsyncIterator

from lab_platform.config import HardwareBenchSettings
from lab_platform.models import BackendProgress, FirmwareInput
from lab_platform.real_backend.errors import (
    EsptoolFlashFailedError,
    EsptoolNotAvailableError,
    EsptoolTimeoutError,
    ProcessExecutableNotFoundError,
    ProcessExecutionTimeoutError,
)
from lab_platform.real_backend.process_runner import ProcessLine, ProcessResult, ProcessRunner
from lab_platform.real_backend.targets.esp32.parser import parse_esptool_progress


def build_flash_args(
    config: HardwareBenchSettings,
    port: str,
    firmware: FirmwareInput,
) -> list[str]:
    return [
        sys.executable,
        "-m",
        "esptool",
        "--chip",
        config.flash.chip,
        "--port",
        port,
        "--baud",
        str(config.flash.baud_rate),
        "--before",
        config.flash.reset_mode,
        "--after",
        config.flash.after,
        "write_flash",
        config.flash_address,
        str(firmware.local_path),
    ]


class Esp32Flasher:
    def __init__(self, config: HardwareBenchSettings, runner: ProcessRunner) -> None:
        self._config = config
        self._runner = runner

    async def flash(self, port: str, firmware: FirmwareInput) -> AsyncIterator[BackendProgress]:
        queue: asyncio.Queue[ProcessLine] = asyncio.Queue(maxsize=256)

        async def capture(line: ProcessLine) -> None:
            await queue.put(line)

        task = asyncio.create_task(
            self._runner.run(
                build_flash_args(self._config, port, firmware),
                timeout_seconds=self._config.flash.timeout_seconds,
                on_output=capture,
            )
        )
        last_percent = 20
        try:
            while not task.done() or not queue.empty():
                try:
                    output = await asyncio.wait_for(queue.get(), timeout=0.05)
                except TimeoutError:
                    continue
                parsed = parse_esptool_progress(output.text)
                if parsed is None:
                    message = f"esptool {output.stream}: {output.text}"
                else:
                    percent, stage = parsed
                    last_percent = max(last_percent, percent)
                    message = f"{stage} — esptool {output.stream}: {output.text}"
                yield BackendProgress(percent=last_percent, message=message)
            result = await task
        except asyncio.CancelledError:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            raise
        except ProcessExecutableNotFoundError as exc:
            raise EsptoolNotAvailableError(
                "esptool is not installed in the Agent Python environment."
            ) from exc
        except ProcessExecutionTimeoutError as exc:
            raise EsptoolTimeoutError(
                f"esptool exceeded {self._config.flash.timeout_seconds:g} seconds."
            ) from exc
        finally:
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        if result.returncode != 0:
            raise EsptoolFlashFailedError(
                _failure_message(result),
                return_code=result.returncode,
            )
        if last_percent < 90:
            yield BackendProgress(percent=90, message="Verifying firmware")


def _failure_message(result: ProcessResult) -> str:
    output = [*result.stderr, *result.stdout]
    detail = next((line for line in reversed(output) if line.strip()), "unknown esptool error")
    return f"esptool failed: {detail}"
