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


class PlatformConfig(ConfigModel):
    agent: AgentSettings = Field(default_factory=AgentSettings)
    simlab: SimLabSettings = Field(default_factory=SimLabSettings)
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
