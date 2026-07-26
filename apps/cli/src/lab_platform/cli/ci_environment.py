from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from uuid import uuid4


class CiProvider(StrEnum):
    GITHUB_ACTIONS = "github_actions"
    GITLAB_CI = "gitlab_ci"
    JENKINS = "jenkins"
    LOCAL = "local"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class CiEnvironment:
    provider: CiProvider
    external_run_id: str
    repository: str | None = None
    ref: str | None = None
    commit_sha: str | None = None
    actor: str | None = None
    attempt: str = "1"

    def as_payload(self) -> dict[str, str | None]:
        return {
            "provider": self.provider.value,
            "external_run_id": self.external_run_id,
            "repository": self.repository,
            "ref": self.ref,
            "commit_sha": self.commit_sha,
            "actor": self.actor,
        }

    @property
    def idempotency_key(self) -> str:
        repository = self.repository or "-"
        return f"{self.provider.value}:{repository}:{self.external_run_id}:{self.attempt}"


def detect_ci_environment(environment: Mapping[str, str] | None = None) -> CiEnvironment:
    """Detect provider metadata without leaking provider rules into domain services."""

    values: Mapping[str, str] = os.environ if environment is None else environment
    if _enabled(values.get("GITHUB_ACTIONS")):
        return CiEnvironment(
            provider=CiProvider.GITHUB_ACTIONS,
            external_run_id=_value(values, "GITHUB_RUN_ID", fallback="unknown"),
            repository=_optional(values, "GITHUB_REPOSITORY"),
            ref=_optional(values, "GITHUB_REF"),
            commit_sha=_optional(values, "GITHUB_SHA"),
            actor=_optional(values, "GITHUB_ACTOR"),
            attempt=_value(values, "GITHUB_RUN_ATTEMPT", fallback="1"),
        )
    if _enabled(values.get("GITLAB_CI")):
        return CiEnvironment(
            provider=CiProvider.GITLAB_CI,
            external_run_id=_value(values, "CI_PIPELINE_ID", fallback="unknown"),
            repository=_optional(values, "CI_PROJECT_PATH"),
            ref=_optional(values, "CI_COMMIT_REF_NAME"),
            commit_sha=_optional(values, "CI_COMMIT_SHA"),
            actor=_optional(values, "GITLAB_USER_LOGIN"),
            attempt=_value(values, "CI_JOB_ID", fallback="1"),
        )
    if _optional(values, "JENKINS_URL") is not None:
        return CiEnvironment(
            provider=CiProvider.JENKINS,
            external_run_id=_value(values, "BUILD_ID", fallback="unknown"),
            repository=_optional(values, "JOB_NAME"),
            ref=_first(values, "BRANCH_NAME", "GIT_BRANCH"),
            commit_sha=_optional(values, "GIT_COMMIT"),
            actor=_optional(values, "BUILD_USER_ID"),
            attempt=_value(values, "BUILD_NUMBER", fallback="1"),
        )

    provider = CiProvider.UNKNOWN if _enabled(values.get("CI")) else CiProvider.LOCAL
    return CiEnvironment(
        provider=provider,
        external_run_id=_value(
            values,
            "LAB_PLATFORM_CI_RUN_ID",
            fallback=f"local-{uuid4().hex}",
        ),
        repository=_optional(values, "LAB_PLATFORM_CI_REPOSITORY"),
        ref=_optional(values, "LAB_PLATFORM_CI_REF"),
        commit_sha=_optional(values, "LAB_PLATFORM_CI_COMMIT_SHA"),
        actor=_optional(values, "LAB_PLATFORM_CI_ACTOR"),
        attempt=_value(values, "LAB_PLATFORM_CI_ATTEMPT", fallback="1"),
    )


def _enabled(value: str | None) -> bool:
    return value is not None and value.strip().casefold() not in {"", "0", "false", "no", "off"}


def _optional(values: Mapping[str, str], name: str) -> str | None:
    value = values.get(name)
    if value is None or not value.strip():
        return None
    return value.strip()


def _value(values: Mapping[str, str], name: str, *, fallback: str) -> str:
    return _optional(values, name) or fallback


def _first(values: Mapping[str, str], *names: str) -> str | None:
    for name in names:
        if (value := _optional(values, name)) is not None:
            return value
    return None
