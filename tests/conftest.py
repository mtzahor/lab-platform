from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOTS = (
    ROOT / "apps/agent/src",
    ROOT / "apps/cli/src",
    ROOT / "packages/config/src",
    ROOT / "packages/core/src",
    ROOT / "packages/logging/src",
    ROOT / "packages/models/src",
    ROOT / "packages/plugins/src",
    ROOT / "packages/simlab/src",
)

for source_root in SOURCE_ROOTS:
    sys.path.insert(0, str(source_root))
