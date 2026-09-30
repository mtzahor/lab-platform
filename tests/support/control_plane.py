from __future__ import annotations

from pathlib import Path

from lab_platform.control_plane import ControlPlaneConfig, ControlPlaneRuntime
from lab_platform.core.oidc import OidcProvider


def make_control_plane_runtime(
    tmp_path: Path,
    *,
    development_enabled: bool = False,
    overrides: dict[str, object] | None = None,
    oidc_provider: OidcProvider | None = None,
) -> ControlPlaneRuntime:
    """Build an isolated loopback runtime, replacing whole config sections for overrides."""
    development: dict[str, object] = {"allow_insecure_agent_transport": True}
    if development_enabled:
        development["enabled"] = True
    config: dict[str, object] = {
        "control_plane": {
            "host": "127.0.0.1",
            "port": 8443,
            "public_url": "http://127.0.0.1:8443",
        },
        "database": {"url": f"sqlite:///{tmp_path / 'control-plane.db'}"},
        "artifacts": {"directory": tmp_path / "artifacts"},
        "development": development,
    }
    if overrides is not None:
        config.update(overrides)
    return ControlPlaneRuntime(
        ControlPlaneConfig.model_validate(config), oidc_provider=oidc_provider
    )
