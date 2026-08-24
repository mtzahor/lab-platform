from __future__ import annotations

from enum import StrEnum
from typing import Protocol, runtime_checkable


class Feature(StrEnum):
    """Commercial extension points intentionally kept outside the open core.

    Core laboratory behavior is deliberately absent from this enum. Reservations,
    workflows, CI, RBAC, audit logging, Agents, SimLab, and the plugin SDK are
    community functionality and must never depend on a commercial provider.
    """

    ADVANCED_AUDIT_RETENTION = "advanced_audit_retention"
    ADVANCED_OIDC_CONTROLS = "advanced_oidc_controls"
    AUTOMATED_BACKUPS = "automated_backups"
    EXTENDED_ARTIFACT_RETENTION = "extended_artifact_retention"
    MANAGED_AGENT_UPDATE_CHANNELS = "managed_agent_update_channels"
    MANAGED_UPGRADES = "managed_upgrades"
    MULTI_SITE_ADMINISTRATION = "multi_site_administration"
    ORGANISATION_POLICIES = "organisation_policies"
    SCALE_OBSERVABILITY = "scale_observability"


@runtime_checkable
class FeatureProvider(Protocol):
    """Stable seam through which a separately distributed edition may add value."""

    @property
    def edition(self) -> str: ...

    def enabled(self, feature: Feature) -> bool: ...


class CommunityFeatureProvider:
    """Default provider used by the independently buildable Apache-2.0 edition."""

    @property
    def edition(self) -> str:
        return "community"

    def enabled(self, feature: Feature) -> bool:
        if not isinstance(feature, Feature):
            raise TypeError("feature must be a Feature value")
        return False


__all__ = ["CommunityFeatureProvider", "Feature", "FeatureProvider"]
