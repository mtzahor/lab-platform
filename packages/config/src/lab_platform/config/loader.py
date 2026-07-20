from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import AliasChoices, BaseModel, ConfigDict, Field, field_validator, model_validator


class ConfigModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)


class AgentSettings(ConfigModel):
    name: str = "local-agent"
    host: str = "127.0.0.1"
    port: int = Field(default=8080, ge=1, le=65535)
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"


class SimLabSettings(ConfigModel):
    enabled: bool = True
    benches: int = Field(default=5, ge=0, le=500)
    auto_start: bool = True
    clock_mode: Literal["manual", "accelerated"] = "accelerated"
    speed_multiplier: float = Field(default=20.0, gt=0, le=1000)
    flash_duration_seconds: float = Field(default=5.0, ge=0, le=3600)


class BackendSettings(ConfigModel):
    type: Literal["simlab", "real"] = "simlab"


class UsbMatchSettings(ConfigModel):
    vendor_id: int | None = Field(
        default=None,
        validation_alias=AliasChoices("vendor_id", "vid"),
        ge=0,
        le=0xFFFF,
    )
    product_id: int | None = Field(
        default=None,
        validation_alias=AliasChoices("product_id", "pid"),
        ge=0,
        le=0xFFFF,
    )
    serial_number: str | None = None


class SerialConnectionSettings(ConfigModel):
    serial_port: str = "auto"
    baud_rate: int = Field(default=115200, ge=300, le=4_000_000)
    usb: UsbMatchSettings = Field(default_factory=UsbMatchSettings)


class Esp32FlashSettings(ConfigModel):
    tool: Literal["esptool"] = "esptool"
    chip: str = "esp32"
    baud_rate: int = Field(default=460800, ge=300, le=4_000_000)
    flash_address: str = "0x10000"
    reset_mode: str = "default_reset"
    after: str = "hard_reset"
    timeout_seconds: float = Field(default=120, gt=0, le=3600)

    @field_validator("flash_address")
    @classmethod
    def validate_flash_address(cls, value: str) -> str:
        try:
            address = int(value, 0)
        except ValueError as exc:
            raise ValueError("flash_address must be an integer such as 0x10000") from exc
        if address < 0:
            raise ValueError("flash_address cannot be negative")
        return value


class FirmwareFormatSettings(ConfigModel):
    format: Literal["raw_bin"] = "raw_bin"
    flash_address: str | None = None

    @field_validator("flash_address")
    @classmethod
    def validate_flash_address(cls, value: str | None) -> str | None:
        if value is not None:
            Esp32FlashSettings.validate_flash_address(value)
        return value


class BootSettings(ConfigModel):
    ready_pattern: str = "^READY$"
    version_pattern: str = r"^FIRMWARE_VERSION=(?P<version>.+)$"
    failure_patterns: list[str] = Field(
        default_factory=lambda: [
            "Guru Meditation Error",
            r"abort\(\)",
            "Brownout detector was triggered",
        ]
    )
    timeout_seconds: float = Field(default=20, gt=0, le=3600)


class HardwareBenchSettings(ConfigModel):
    id: str = Field(min_length=1, max_length=200)
    name: str = Field(min_length=1, max_length=200)
    target_type: str = "esp32"
    connection: SerialConnectionSettings = Field(default_factory=SerialConnectionSettings)
    flash: Esp32FlashSettings = Field(default_factory=Esp32FlashSettings)
    firmware: FirmwareFormatSettings = Field(default_factory=FirmwareFormatSettings)
    boot: BootSettings = Field(default_factory=BootSettings)

    @property
    def flash_address(self) -> str:
        return self.firmware.flash_address or self.flash.flash_address


class HardwareSettings(ConfigModel):
    benches: list[HardwareBenchSettings] = Field(default_factory=list, max_length=1)

    @model_validator(mode="after")
    def unique_bench_ids(self) -> HardwareSettings:
        identifiers = [bench.id for bench in self.benches]
        if len(identifiers) != len(set(identifiers)):
            raise ValueError("hardware bench ids must be unique")
        return self


class DatabaseSettings(ConfigModel):
    url: str = "sqlite:///./.lab-platform/lab.db"


class ArtifactSettings(ConfigModel):
    directory: Path = Path("./.lab-platform/artifacts")
    max_firmware_size_mb: int = Field(default=100, ge=1, le=4096)


class OperationSettings(ConfigModel):
    poll_interval_ms: int = Field(default=250, ge=10, le=60_000)
    shutdown_timeout_seconds: float = Field(default=10.0, ge=0, le=300)


class DevelopmentSettings(ConfigModel):
    enable_simlab_controls: bool = False


class PlatformConfig(ConfigModel):
    agent: AgentSettings = Field(default_factory=AgentSettings)
    backend: BackendSettings = Field(default_factory=BackendSettings)
    simlab: SimLabSettings = Field(default_factory=SimLabSettings)
    hardware: HardwareSettings = Field(default_factory=HardwareSettings)
    database: DatabaseSettings = Field(default_factory=DatabaseSettings)
    artifacts: ArtifactSettings = Field(default_factory=ArtifactSettings)
    operations: OperationSettings = Field(default_factory=OperationSettings)
    development: DevelopmentSettings = Field(default_factory=DevelopmentSettings)
    plugins: list[str] = Field(default_factory=lambda: ["power", "serial", "firmware"])


def load_config(config_dir: str | Path = "config") -> PlatformConfig:
    root = Path(config_dir)
    if root.is_file():
        return PlatformConfig.model_validate(dict(_read_yaml_file(root)))
    data: dict[str, Any] = {}
    for filename in ("agent.yaml", "simlab.yaml", "hardware.yaml"):
        data = _deep_merge(data, _read_yaml_file(root / filename))
    return PlatformConfig.model_validate(data)


def validate_config(config_dir: str | Path = "config") -> PlatformConfig:
    return load_config(config_dir)


def _read_yaml_file(path: Path) -> Mapping[str, Any]:
    if not path.exists():
        return {}
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ValueError(f"Invalid YAML in {path}: {exc}") from exc
    if raw is None:
        return {}
    if not isinstance(raw, Mapping):
        raise ValueError(f"Expected a mapping in {path}")
    return raw


def _deep_merge(base: Mapping[str, Any], update: Mapping[str, Any]) -> dict[str, Any]:
    merged: dict[str, Any] = dict(base)
    for key, value in update.items():
        existing = merged.get(key)
        if isinstance(existing, Mapping) and isinstance(value, Mapping):
            merged[key] = _deep_merge(existing, value)
        else:
            merged[key] = value
    return merged
