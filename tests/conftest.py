from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOTS = (
    ROOT / "apps/agent/src",
    ROOT / "apps/cli/src",
    ROOT / "apps/control_plane/src",
    ROOT / "packages/agent_protocol/src",
    ROOT / "packages/agent_runtime/src",
    ROOT / "packages/config/src",
    ROOT / "packages/control_plane_core/src",
    ROOT / "packages/core/src",
    ROOT / "packages/logging/src",
    ROOT / "packages/models/src",
    ROOT / "packages/persistence/src",
    ROOT / "packages/plugins/src",
    ROOT / "packages/real_backend/src",
    ROOT / "packages/simlab/src",
    ROOT / "packages/simlab_adapter/src",
)

for source_root in SOURCE_ROOTS:
    sys.path.insert(0, str(source_root))
