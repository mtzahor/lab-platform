from __future__ import annotations

import pytest
from lab_platform.core.features import CommunityFeatureProvider, Feature, FeatureProvider


def test_community_provider_is_an_explicit_independently_buildable_boundary() -> None:
    provider = CommunityFeatureProvider()

    assert isinstance(provider, FeatureProvider)
    assert provider.edition == "community"
    assert all(not provider.enabled(feature) for feature in Feature)


def test_community_provider_rejects_untyped_feature_names() -> None:
    with pytest.raises(TypeError, match="Feature"):
        CommunityFeatureProvider().enabled("managed_upgrades")  # type: ignore[arg-type]
