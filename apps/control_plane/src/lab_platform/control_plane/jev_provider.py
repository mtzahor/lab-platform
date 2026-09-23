from __future__ import annotations

import asyncio
import importlib
import math
from collections.abc import Awaitable, Callable, Mapping
from importlib.metadata import PackageNotFoundError, version
from typing import Any, cast

from lab_platform.core.decision_engine.interface import DiagnosisUnavailable
from lab_platform.core.decision_engine.schema import (
    ACTIONS,
    DIAGNOSES,
    SEVERITIES,
    SEVERITY_RUBRIC,
    questions,
)
from lab_platform.core.decision_engine.settings import DecisionSettings
from lab_platform.models.decisions import (
    DecisionConfidence,
    DiagnosticDecision,
    DiagnosticState,
    Severity,
)

BatchRequest = Callable[[str, dict[str, Any]], Awaitable[Mapping[str, Any]]]


class JevDecisionEngine:
    """The only module that imports or handles Jev SDK objects."""

    def __init__(self, settings: DecisionSettings, *, request: BatchRequest | None = None) -> None:
        self.settings = settings
        self._request = request or self._sdk_request

    async def diagnose_run(self, run_state: DiagnosticState) -> DiagnosticDecision:
        if not self.settings.enabled:
            raise DiagnosisUnavailable("disabled")
        try:
            async with asyncio.timeout(self.settings.timeout_ms / 1000):
                response = await self._request(run_state.text, questions())
            return parse_response(response)
        except DiagnosisUnavailable:
            raise
        except TimeoutError as exc:
            raise DiagnosisUnavailable("timeout") from exc
        except Exception as exc:
            status = getattr(exc, "status", getattr(exc, "status_code", None))
            code = (
                {
                    401: "authentication_failed",
                    403: "authentication_failed",
                    429: "rate_limited",
                }.get(status)
                if isinstance(status, int)
                else None
            )
            if code is None:
                name = type(exc).__name__.lower()
                code = "timeout" if "timeout" in name else "service_unavailable"
            raise DiagnosisUnavailable(code) from exc

    async def _sdk_request(self, state: str, batch: dict[str, Any]) -> Mapping[str, Any]:
        key = self.settings.api_key.get_secret_value().strip()
        if not key or not key.isascii() or any(ord(c) < 33 or ord(c) == 127 for c in key):
            raise DiagnosisUnavailable("invalid_configuration")
        try:
            sdk = importlib.import_module("typesafe_sdk")
        except ImportError as exc:
            raise DiagnosisUnavailable("sdk_not_installed") from exc
        # Explicit URL avoids implicit SDK gateway/env overrides of the data destination.
        async with sdk.AsyncTypeSafeClient(
            api_key=key,
            base_url="https://api.typesafe.ai",
            model=self.settings.model,
            retry=sdk.RetryPolicy(max_retries=0, timeout=self.settings.timeout_ms / 1000),
        ) as client:
            response = await client.system_one(state=state, questions=batch)
        payload: dict[str, Any] = response.model_dump(mode="json")
        try:
            payload["sdk_version"] = version("typesafe-sdk")
        except PackageNotFoundError:
            payload["sdk_version"] = "unknown"
        return payload


def _probability(value: object) -> float:
    numeric = (
        float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else -1.0
    )
    if (
        not isinstance(value, (int, float))
        or isinstance(value, bool)
        or not math.isfinite(numeric)
        or not 0 <= numeric <= 1
    ):
        raise ValueError("invalid probability")
    return numeric


def _distribution(value: object, labels: set[str], *, sparse: bool = False) -> dict[str, float]:
    if not isinstance(value, dict) or not value or not set(value) <= labels:
        raise ValueError("invalid labels")
    if not sparse and set(value) != labels:
        raise ValueError("missing probabilities")
    result = {key: _probability(probability) for key, probability in value.items()}
    if not math.isclose(sum(result.values()), 1.0, abs_tol=0.02):
        raise ValueError("invalid distribution")
    return result


def parse_response(response: Mapping[str, Any]) -> DiagnosticDecision:
    try:
        answers = response["answers"]
        if not isinstance(answers, dict) or set(answers) != set(questions()):
            raise ValueError("missing or unexpected answers")
        confidence = {}
        distributions = {}
        selected = {}
        for name, labels in (("diagnosis", DIAGNOSES), ("recommended_action", ACTIONS)):
            answer = answers[name]
            if answer["type"] != "choice" or answer["choice"] not in labels:
                raise ValueError("unexpected choice")
            distribution = _distribution(answer["probabilities"], set(labels))
            if distribution[answer["choice"]] < max(distribution.values()):
                raise ValueError("inconsistent choice")
            selected[name] = answer["choice"]
            confidence[name] = _probability(answer["confidence"])
            distributions[name] = distribution
        severity = answers["severity"]
        if severity["type"] != "score":
            raise ValueError("invalid severity type")
        distribution = _distribution(
            severity["probabilities"], {str(i) for i in range(5)}, sparse=True
        )
        score = severity["score"]
        if type(score) not in (int, float) or not math.isfinite(score) or not 0 <= score <= 4:
            raise ValueError("invalid score")
        if abs(sum(int(i) * p for i, p in distribution.items()) - score) > 0.1:
            raise ValueError("inconsistent score")
        if severity["legend"] != {str(i): label for i, label in enumerate(SEVERITY_RUBRIC)}:
            raise ValueError("unexpected severity rubric")
        # Score is ordinal. Display the most likely level; ties prefer higher severity.
        level = max(range(5), key=lambda i: (distribution.get(str(i), 0), i))
        confidence["severity"] = _probability(severity["confidence"])
        distributions["severity"] = distribution
        retry = answers["retry_safe"]
        if retry["type"] != "noul":
            raise ValueError("invalid retry type")
        model = response["model"]
        if not isinstance(model, str) or not model or len(model) > 200:
            raise ValueError("invalid model")
        metadata: dict[str, Any] = {
            "severity_score": float(score),
            "severity_legend": severity["legend"],
        }
        if isinstance(response.get("sdk_version"), str):
            metadata["sdk_version"] = response["sdk_version"][:40]
        return DiagnosticDecision(
            classification=selected["diagnosis"],
            recommended_action=selected["recommended_action"],
            severity=cast(Severity, SEVERITIES[level]),
            retry_safe_probability=_probability(retry["noul"]),
            confidence=DecisionConfidence.model_validate(confidence),
            probability_distribution=distributions,
            provider="jev",
            model=model,
            raw_metadata=metadata,
        )
    except (KeyError, TypeError, ValueError, AttributeError) as exc:
        raise DiagnosisUnavailable("malformed_response") from exc
