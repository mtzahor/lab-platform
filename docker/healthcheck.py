#!/usr/bin/env python3
"""Dependency-free HTTP health probe used by the official containers."""

from __future__ import annotations

import os
import sys
import urllib.error
import urllib.request


def main() -> int:
    url = os.environ.get(
        "LAB_HEALTHCHECK_URL",
        "http://127.0.0.1:8443/health/live",
    )
    try:
        with urllib.request.urlopen(url, timeout=3) as response:  # noqa: S310 - operator URL
            if 200 <= response.status < 300:
                return 0
            print(f"health probe returned HTTP {response.status}: {url}", file=sys.stderr)
    except (OSError, urllib.error.URLError) as exc:
        print(f"health probe failed for {url}: {exc}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
