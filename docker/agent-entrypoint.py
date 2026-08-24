#!/usr/bin/env python3
"""Load an Agent credential from a mounted file, then exec ``lab-agent``."""

from __future__ import annotations

import os
import sys
from pathlib import Path


def main() -> None:
    credential_file = os.environ.get("LAB_AGENT_CREDENTIAL_FILE")
    credential_environment = os.environ.get(
        "LAB_AGENT_CREDENTIAL_ENV",
        "LAB_AGENT_CREDENTIAL",
    )
    if credential_file:
        credential = Path(credential_file).read_text(encoding="utf-8").strip()
        if not credential:
            raise SystemExit("Agent credential file is empty")
        os.environ[credential_environment] = credential
    os.execvpe("lab-agent", ["lab-agent", *sys.argv[1:]], os.environ)


if __name__ == "__main__":
    main()
