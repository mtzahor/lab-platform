from __future__ import annotations

import os
from collections.abc import Mapping
from typing import Literal

from lab_platform.models.domain import LabModel
from pydantic import Field, SecretStr, ValidationError


class DecisionSettings(LabModel):
    enabled: bool = False
    api_key: SecretStr = Field(default=SecretStr(""), exclude=True)
    model: str = Field(default="jev-latest", min_length=1, max_length=200)
    timeout_ms: int = Field(default=5000, ge=100, le=30000)
    diagnosis_min_confidence: float = Field(default=0.90, ge=0, le=1, allow_inf_nan=False)
    action_min_confidence: float = Field(default=0.95, ge=0, le=1, allow_inf_nan=False)
    retry_safe_min_probability: float = Field(default=0.99, ge=0, le=1, allow_inf_nan=False)
    mode: Literal["recommend", "shadow"] = "recommend"
    max_state_bytes: int = Field(default=32768, ge=1024, le=65536)
    configuration_error: str | None = None

    def with_environment(self, environ: Mapping[str, str] | None = None) -> DecisionSettings:
        source = os.environ if environ is None else environ
        values = self.model_dump()
        values["api_key"] = self.api_key
        for field in type(self).model_fields:
            if field == "configuration_error":
                continue
            raw = source.get("JEV_" + field.upper())
            if raw is not None:
                values[field] = raw
        try:
            return type(self).model_validate(values)
        except ValidationError:
            # Invalid experimental configuration must not prevent platform startup.
            return type(self)(configuration_error="invalid_configuration")
