from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field


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
    type: Literal["simlab"] = "simlab"


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
    database: DatabaseSettings = Field(default_factory=DatabaseSettings)
    artifacts: ArtifactSettings = Field(default_factory=ArtifactSettings)
    operations: OperationSettings = Field(default_factory=OperationSettings)
    development: DevelopmentSettings = Field(default_factory=DevelopmentSettings)
    plugins: list[str] = Field(default_factory=lambda: ["power", "serial", "firmware"])


def load_config(config_dir: str | Path = "config") -> PlatformConfig:
    root = Path(config_dir)
    data: dict[str, Any] = {}
    for filename in ("agent.yaml", "simlab.yaml"):
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
