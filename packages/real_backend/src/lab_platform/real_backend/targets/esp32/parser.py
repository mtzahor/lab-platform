from __future__ import annotations

import re

_WRITE_PROGRESS = re.compile(r"(?P<percent>\d{1,3})(?:\.\d+)?\s*%")
_CHIP_PATTERNS = (
    re.compile(r"Chip type:\s*(?P<chip>[^\r\n(]+)", re.IGNORECASE),
    re.compile(r"Chip is\s+(?P<chip>[^\r\n(]+)", re.IGNORECASE),
    re.compile(r"Detecting chip type\.\.\.\s*(?P<chip>[^\r\n]+)", re.IGNORECASE),
)
_MAC_PATTERN = re.compile(
    r"(?:MAC|MAC address):\s*(?P<mac>(?:[0-9a-f]{2}:){5}[0-9a-f]{2})",
    re.IGNORECASE,
)
_WRONG_CHIP_PATTERN = re.compile(
    r"(?:This chip is|Detected chip type:)\s*(?P<chip>ESP[\w-]+)(?:,|\s)",
    re.IGNORECASE,
)


def parse_esptool_progress(line: str) -> tuple[int, str] | None:
    normalized = line.lower()
    if "connecting" in normalized:
        return 20, "Connecting to bootloader"
    if "eras" in normalized:
        return 30, "Erasing flash region"
    if "writing at" in normalized:
        match = _WRITE_PROGRESS.search(line)
        raw = min(100, int(match.group("percent"))) if match else 0
        return 40 + round(raw * 0.45), "Writing firmware"
    if "hash of data verified" in normalized or "verified" in normalized:
        return 90, "Verifying firmware"
    return None


def parse_chip_info(output: str) -> tuple[str | None, str | None]:
    chip: str | None = None
    for pattern in _CHIP_PATTERNS:
        match = pattern.search(output)
        if match:
            chip = match.group("chip").strip()
            break
    mac_match = _MAC_PATTERN.search(output)
    mac = mac_match.group("mac").upper() if mac_match else None
    return chip, mac


def parse_wrong_chip(output: str) -> str | None:
    match = _WRONG_CHIP_PATTERN.search(output)
    return match.group("chip") if match else None


def extract_firmware_version(line: str, pattern: str) -> str | None:
    match = re.search(pattern, line)
    if match is None:
        return None
    try:
        return match.group("version")
    except (IndexError, KeyError):
        return match.group(0)


def matches_any(line: str, patterns: list[str]) -> str | None:
    for pattern in patterns:
        if re.search(pattern, line):
            return pattern
    return None
