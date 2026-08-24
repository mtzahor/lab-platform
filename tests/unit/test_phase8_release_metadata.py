from __future__ import annotations

import pytest
from lab_platform.core.release import build_metadata, release_channel


@pytest.mark.parametrize(
    ("version", "expected"),
    [
        ("1.0.0", "stable"),
        ("0.9.0-beta", "preview"),
        ("0.9.0rc1", "preview"),
        ("0.9.0-dev.42", "nightly"),
    ],
)
def test_release_channel_is_derived_from_semantic_prerelease(version: str, expected: str) -> None:
    assert release_channel(version, None) == expected


def test_release_metadata_accepts_only_safe_reproducible_build_labels() -> None:
    metadata = build_metadata(
        {
            "LAB_RELEASE_CHANNEL": "preview",
            "LAB_BUILD_COMMIT": "ABCDEF0123456789",
            "LAB_BUILD_DATE": "2026-08-23T10:11:12Z",
        }
    )

    assert metadata.release_channel == "preview"
    assert metadata.commit == "abcdef0123456789"
    assert metadata.built_at == "2026-08-23T10:11:12Z"
    assert metadata.as_dict()["version"] == metadata.version

    unsafe = build_metadata(
        {
            "LAB_BUILD_COMMIT": "$(do-not-execute)",
            "LAB_BUILD_DATE": "today",
        }
    )
    assert unsafe.commit is None
    assert unsafe.built_at is None


def test_invalid_explicit_release_channel_fails_closed() -> None:
    with pytest.raises(ValueError, match="stable, preview, or nightly"):
        release_channel("1.0.0", "enterprise")
