from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Annotated, Any, Literal, TypeAlias

import yaml
from pydantic import AliasChoices, BaseModel, ConfigDict, Field, field_validator, model_validator


class ConfigModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)


class AgentSettings(ConfigModel):
    name: str = "local-agent"
    host: str = "127.0.0.1"
    port: int = Field(default=8080, ge=1, le=65535)
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"
    max_request_body_size_mb: int = Field(default=1, ge=1, le=1024)


class SimLabSettings(ConfigModel):
    enabled: bool = True
    benches: int = Field(default=5, ge=0, le=500)
    bench_prefix: str = Field(
        default="bench",
        min_length=1,
        max_length=100,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$",
    )
    auto_start: bool = True
    clock_mode: Literal["manual", "accelerated"] = "accelerated"
    speed_multiplier: float = Field(default=20.0, gt=0, le=1000)
    flash_duration_seconds: float = Field(default=5.0, ge=0, le=3600)
    labels: dict[str, str] = Field(default_factory=dict)


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
    labels: dict[str, str] = Field(default_factory=dict)

    @property
    def flash_address(self) -> str:
        return self.firmware.flash_address or self.flash.flash_address


class HardwareSettings(ConfigModel):
    benches: list[HardwareBenchSettings] = Field(default_factory=list, max_length=500)

    @model_validator(mode="after")
    def unique_bench_ids(self) -> HardwareSettings:
        identifiers = [bench.id for bench in self.benches]
        if len(identifiers) != len(set(identifiers)):
            raise ValueError("hardware bench ids must be unique")
        return self


class SimLabBackendConfig(ConfigModel):
    """Configuration for one SimLab backend instance."""

    config_path: Path | None = None
    enabled: bool = True
    bench_count: int = Field(
        default=5,
        validation_alias=AliasChoices("bench_count", "benches"),
        ge=0,
        le=500,
    )
    bench_prefix: str = Field(
        default="bench",
        min_length=1,
        max_length=100,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$",
    )
    auto_start: bool = True
    clock_mode: Literal["manual", "accelerated"] = "accelerated"
    speed_multiplier: float = Field(default=20.0, gt=0, le=1000)
    flash_duration_seconds: float = Field(default=5.0, ge=0, le=3600)
    labels: dict[str, str] = Field(default_factory=dict)


class RealBackendConfig(HardwareSettings):
    """Configuration for one physical-lab backend instance."""


class SimLabBackendSettings(ConfigModel):
    id: str = Field(min_length=1, max_length=100, pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
    type: Literal["simlab"]
    config: SimLabBackendConfig = Field(default_factory=SimLabBackendConfig)


class RealBackendSettings(ConfigModel):
    id: str = Field(min_length=1, max_length=100, pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
    type: Literal["real"]
    config: RealBackendConfig


BackendInstanceSettings: TypeAlias = Annotated[
    SimLabBackendSettings | RealBackendSettings,
    Field(discriminator="type"),
]


class DatabaseSettings(ConfigModel):
    url: str = "sqlite:///./.lab-platform/lab.db"


class ArtifactSettings(ConfigModel):
    directory: Path = Path("./.lab-platform/artifacts")
    max_firmware_size_mb: int = Field(default=100, ge=1, le=4096)
    max_upload_size_mb: int = Field(default=100, ge=1, le=4096)
    retention_days: int | None = Field(default=None, ge=1, le=3650)


class CiSettings(ConfigModel):
    default_reservation_minutes: int = Field(default=30, ge=1, le=10_080)
    maximum_reservation_minutes: int = Field(default=120, ge=1, le=10_080)
    heartbeat_interval_seconds: int = Field(default=30, ge=1, le=3600)
    heartbeat_timeout_seconds: int = Field(default=120, ge=2, le=86_400)
    bench_wait_timeout_seconds: int = Field(default=600, ge=0, le=86_400)
    session_timeout_seconds: int = Field(default=3600, ge=1, le=604_800)
    workflow_timeout_seconds: int = Field(default=1800, ge=1, le=604_800)
    step_timeout_seconds: int = Field(default=600, ge=1, le=86_400)
    cleanup_timeout_seconds: int = Field(default=60, ge=1, le=3600)
    reaper_poll_interval_seconds: float = Field(default=5.0, gt=0, le=3600)

    @model_validator(mode="after")
    def validate_ci_limits(self) -> CiSettings:
        if self.default_reservation_minutes > self.maximum_reservation_minutes:
            raise ValueError("default CI reservation duration cannot exceed maximum duration")
        if self.heartbeat_interval_seconds >= self.heartbeat_timeout_seconds:
            raise ValueError("CI heartbeat interval must be shorter than heartbeat timeout")
        if self.workflow_timeout_seconds > self.session_timeout_seconds:
            raise ValueError("CI workflow timeout cannot exceed session timeout")
        return self


class SerialStreamSettings(ConfigModel):
    stream_buffer_lines: int = Field(default=500, ge=1, le=100_000)
    artifact_max_size_mb: int = Field(default=50, ge=1, le=4096)
    decode_errors: Literal["replace", "strict", "ignore"] = "replace"
    redact_patterns: list[str] = Field(default_factory=list, max_length=100)


class OperationSettings(ConfigModel):
    poll_interval_ms: int = Field(default=250, ge=10, le=60_000)
    shutdown_timeout_seconds: float = Field(default=10.0, ge=0, le=300)
    recovery_mode: Literal["mark_interrupted_failed"] = "mark_interrupted_failed"


class ReservationSettings(ConfigModel):
    default_duration_minutes: int = Field(default=30, ge=1, le=10_080)
    maximum_duration_minutes: int = Field(default=240, ge=1, le=10_080)
    expiry_grace_seconds: int = Field(default=30, ge=0, le=3600)
    queue_enabled: bool = True
    scheduled_protection_window_minutes: int = Field(default=5, ge=0, le=1440)

    @model_validator(mode="after")
    def default_does_not_exceed_maximum(self) -> ReservationSettings:
        if self.default_duration_minutes > self.maximum_duration_minutes:
            raise ValueError("default reservation duration cannot exceed maximum duration")
        return self


class SchedulerSettings(ConfigModel):
    poll_interval_seconds: float = Field(default=1.0, gt=0, le=3600)
    automatic_assignment: bool = True


class WorkflowSettings(ConfigModel):
    definitions_directory: Path = Path("./workflows")
    definition_paths: list[Path] = Field(default_factory=list, max_length=1000)


class DevelopmentSettings(ConfigModel):
    enable_simlab_controls: bool = False


class PlatformConfig(ConfigModel):
    agent: AgentSettings = Field(default_factory=AgentSettings)
    backend: BackendSettings = Field(default_factory=BackendSettings)
    backends: list[BackendInstanceSettings] = Field(default_factory=list, max_length=100)
    simlab: SimLabSettings = Field(default_factory=SimLabSettings)
    hardware: HardwareSettings = Field(default_factory=HardwareSettings)
    database: DatabaseSettings = Field(default_factory=DatabaseSettings)
    artifacts: ArtifactSettings = Field(default_factory=ArtifactSettings)
    ci: CiSettings = Field(default_factory=CiSettings)
    serial: SerialStreamSettings = Field(default_factory=SerialStreamSettings)
    operations: OperationSettings = Field(default_factory=OperationSettings)
    reservations: ReservationSettings = Field(default_factory=ReservationSettings)
    scheduler: SchedulerSettings = Field(default_factory=SchedulerSettings)
    workflows: WorkflowSettings = Field(default_factory=WorkflowSettings)
    development: DevelopmentSettings = Field(default_factory=DevelopmentSettings)
    plugins: list[str] = Field(default_factory=lambda: ["power", "serial", "firmware"])

    @model_validator(mode="after")
    def globally_unique_configured_ids(self) -> PlatformConfig:
        backend_ids = [backend.id for backend in self.backends]
        if len(backend_ids) != len(set(backend_ids)):
            raise ValueError("backend ids must be unique")

        bench_owners: dict[str, str] = {}
        for backend in self.backends:
            if not isinstance(backend, RealBackendSettings):
                continue
            for bench in backend.config.benches:
                owner = bench_owners.setdefault(bench.id, backend.id)
                if owner != backend.id:
                    raise ValueError(
                        f"bench id {bench.id!r} is configured by both {owner!r} and {backend.id!r}"
                    )
        return self

    @property
    def effective_backends(self) -> tuple[BackendInstanceSettings, ...]:
        """Return Phase 3 definitions, synthesizing one from the Phase 2 fields."""

        if self.backends:
            return tuple(self.backends)
        if self.backend.type == "real":
            return (
                RealBackendSettings(
                    id="real",
                    type="real",
                    config=RealBackendConfig(benches=self.hardware.benches),
                ),
            )
        return (
            SimLabBackendSettings(
                id="simlab",
                type="simlab",
                config=SimLabBackendConfig(
                    enabled=self.simlab.enabled,
                    bench_count=self.simlab.benches,
                    bench_prefix=self.simlab.bench_prefix,
                    auto_start=self.simlab.auto_start,
                    clock_mode=self.simlab.clock_mode,
                    speed_multiplier=self.simlab.speed_multiplier,
                    flash_duration_seconds=self.simlab.flash_duration_seconds,
                    labels=self.simlab.labels,
                ),
            ),
        )


def load_config(config_dir: str | Path = "config") -> PlatformConfig:
    root = Path(config_dir)
    if root.is_file():
        data = dict(_read_yaml_file(root))
        return PlatformConfig.model_validate(_expand_backend_config_paths(data, root.parent))
    merged_data: dict[str, Any] = {}
    for filename in ("agent.yaml", "simlab.yaml", "hardware.yaml", "backends.yaml"):
        merged_data = _deep_merge(merged_data, _read_yaml_file(root / filename))
    return PlatformConfig.model_validate(_expand_backend_config_paths(merged_data, root))


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


def _expand_backend_config_paths(data: Mapping[str, Any], base: Path) -> dict[str, Any]:
    expanded = dict(data)
    raw_backends = expanded.get("backends")
    if not isinstance(raw_backends, list):
        return expanded
    backends: list[object] = []
    for raw_backend in raw_backends:
        if not isinstance(raw_backend, Mapping) or raw_backend.get("type") != "simlab":
            backends.append(raw_backend)
            continue
        raw_config = raw_backend.get("config")
        if not isinstance(raw_config, Mapping) or not raw_config.get("config_path"):
            backends.append(raw_backend)
            continue
        configured_path = Path(str(raw_config["config_path"]))
        path = configured_path if configured_path.is_absolute() else base / configured_path
        if not path.is_file():
            raise ValueError(f"SimLab config_path does not exist: {path}")
        external = _normalize_simlab_aliases(
            _simlab_config_from_file(path, str(raw_backend.get("id", "")))
        )
        inline = _normalize_simlab_aliases(
            {key: value for key, value in raw_config.items() if key != "config_path"}
        )
        merged_config = _deep_merge(external, inline)
        merged_config["config_path"] = path
        backends.append({**raw_backend, "config": merged_config})
    expanded["backends"] = backends
    return expanded


def _simlab_config_from_file(path: Path, backend_id: str) -> dict[str, Any]:
    payload = dict(_read_yaml_file(path))
    simlab = payload.get("simlab")
    if isinstance(simlab, Mapping):
        return dict(simlab)
    backends = payload.get("backends")
    if isinstance(backends, list):
        candidates = [
            item for item in backends if isinstance(item, Mapping) and item.get("type") == "simlab"
        ]
        selected = next((item for item in candidates if item.get("id") == backend_id), None)
        if selected is None and candidates:
            selected = candidates[0]
        if selected is not None and isinstance(selected.get("config"), Mapping):
            return dict(selected["config"])
    allowed = {
        "enabled",
        "benches",
        "bench_count",
        "bench_prefix",
        "auto_start",
        "clock_mode",
        "speed_multiplier",
        "flash_duration_seconds",
        "labels",
    }
    if payload and set(payload).issubset(allowed):
        return payload
    raise ValueError(f"SimLab config_path has no SimLab configuration: {path}")


def _normalize_simlab_aliases(config: Mapping[str, Any]) -> dict[str, Any]:
    normalized = dict(config)
    if "benches" in normalized:
        if "bench_count" in normalized:
            raise ValueError("SimLab configuration must not set both benches and bench_count")
        normalized["bench_count"] = normalized.pop("benches")
    return normalized
