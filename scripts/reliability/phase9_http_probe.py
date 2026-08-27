#!/usr/bin/env python3
"""Run a bounded HTTP load/soak probe and write a redacted Phase 9 evidence record."""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from threading import Lock
from typing import Any


@dataclass(frozen=True, slots=True)
class HttpSample:
    latency_ms: float
    status_code: int | None
    error: str | None


def percentile(values: Sequence[float], fraction: float) -> float:
    """Return the nearest-rank percentile for non-empty measurements."""

    if not values:
        raise ValueError("cannot calculate a percentile without measurements")
    if not 0 < fraction <= 1:
        raise ValueError("percentile fraction must be greater than 0 and at most 1")
    ordered = sorted(values)
    rank = max(0, math.ceil(fraction * len(ordered)) - 1)
    return ordered[rank]


def validate_target(base_url: str, path: str) -> str:
    """Build an HTTP(S) target without accepting credentials or query secrets."""

    base = urllib.parse.urlsplit(base_url)
    if base.scheme not in {"http", "https"} or not base.netloc:
        raise ValueError("base URL must be an absolute http:// or https:// URL")
    if base.username is not None or base.password is not None:
        raise ValueError("credentials are not allowed in the base URL")
    if base.query or base.fragment:
        raise ValueError("base URL must not contain a query or fragment")
    relative = urllib.parse.urlsplit(path)
    if relative.scheme or relative.netloc or not path.startswith("/"):
        raise ValueError("path must be an absolute URL path on the configured server")
    if relative.query or relative.fragment:
        raise ValueError("probe paths must not contain a query or fragment")
    return urllib.parse.urlunsplit(
        (base.scheme, base.netloc, f"{base.path.rstrip('/')}{path}", "", "")
    )


def request_once(target: str, *, token: str | None, timeout_seconds: float) -> HttpSample:
    headers = {"Accept": "application/json", "User-Agent": "lab-platform-phase9-probe/1"}
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(target, headers=headers, method="GET")
    started = time.perf_counter()
    try:
        with urllib.request.urlopen(request, timeout=timeout_seconds) as response:  # noqa: S310
            response.read(1_000_000)
            status = response.status
        error = None if 200 <= status < 300 else f"unexpected HTTP status {status}"
        return HttpSample(
            latency_ms=(time.perf_counter() - started) * 1000,
            status_code=status,
            error=error,
        )
    except urllib.error.HTTPError as exc:
        exc.read(4096)
        return HttpSample(
            latency_ms=(time.perf_counter() - started) * 1000,
            status_code=exc.code,
            error=f"HTTP {exc.code}",
        )
    except (TimeoutError, urllib.error.URLError, OSError) as exc:
        return HttpSample(
            latency_ms=(time.perf_counter() - started) * 1000,
            status_code=None,
            error=type(exc).__name__,
        )


def run_request_count(
    target: str,
    *,
    request_count: int,
    concurrency: int,
    token: str | None,
    timeout_seconds: float,
) -> list[HttpSample]:
    with ThreadPoolExecutor(max_workers=concurrency) as executor:
        return list(
            executor.map(
                lambda _index: request_once(
                    target,
                    token=token,
                    timeout_seconds=timeout_seconds,
                ),
                range(request_count),
            )
        )


def run_duration(
    target: str,
    *,
    duration_seconds: float,
    concurrency: int,
    requests_per_second: float,
    token: str | None,
    timeout_seconds: float,
) -> list[HttpSample]:
    deadline = time.monotonic() + duration_seconds
    samples: list[HttpSample] = []
    guard = Lock()
    per_worker_interval = concurrency / requests_per_second

    def worker() -> None:
        while time.monotonic() < deadline:
            started = time.monotonic()
            sample = request_once(target, token=token, timeout_seconds=timeout_seconds)
            with guard:
                samples.append(sample)
            remaining = per_worker_interval - (time.monotonic() - started)
            if remaining > 0:
                time.sleep(min(remaining, max(0.0, deadline - time.monotonic())))

    with ThreadPoolExecutor(max_workers=concurrency) as executor:
        futures = [executor.submit(worker) for _index in range(concurrency)]
        for future in futures:
            future.result()
    return samples


def summarize(
    samples: Sequence[HttpSample],
    *,
    profile: str,
    public_target: str,
    started_at: datetime,
    duration_seconds: float,
    concurrency: int,
    maximum_p95_ms: float,
) -> dict[str, Any]:
    latencies = [sample.latency_ms for sample in samples]
    failures = [sample for sample in samples if sample.error is not None]
    p50 = percentile(latencies, 0.50) if latencies else None
    p95 = percentile(latencies, 0.95) if latencies else None
    p99 = percentile(latencies, 0.99) if latencies else None
    passed = bool(samples) and not failures and p95 is not None and p95 <= maximum_p95_ms
    error_counts: dict[str, int] = {}
    for sample in failures:
        assert sample.error is not None
        error_counts[sample.error] = error_counts.get(sample.error, 0) + 1
    return {
        "schema_version": 1,
        "kind": "phase9-http-load-soak-probe",
        "profile": profile,
        "status": "PASS" if passed else "FAIL",
        "scope": (
            "HTTP availability/latency only; this record does not by itself close the Phase 9 "
            "load, soak, recovery, or 1.0 release gates."
        ),
        "started_at": started_at.isoformat(),
        "finished_at": datetime.now(UTC).isoformat(),
        "duration_seconds": round(duration_seconds, 3),
        "target": public_target,
        "concurrency": concurrency,
        "requests": len(samples),
        "failures": len(failures),
        "error_counts": error_counts,
        "latency_ms": {
            "p50": None if p50 is None else round(p50, 3),
            "p95": None if p95 is None else round(p95, 3),
            "p99": None if p99 is None else round(p99, 3),
            "maximum_allowed_p95": maximum_p95_ms,
        },
    }


def _write_json(path: Path, payload: dict[str, Any], *, overwrite: bool) -> None:
    if path.exists() and not overwrite:
        raise FileExistsError(f"refusing to overwrite evidence file: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(path)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--path", default="/api/v1/health")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--requests", type=int)
    mode.add_argument("--duration-seconds", type=float)
    parser.add_argument("--concurrency", type=int, default=20)
    parser.add_argument("--requests-per-second", type=float, default=20)
    parser.add_argument("--timeout-seconds", type=float, default=5)
    parser.add_argument("--maximum-p95-ms", type=float, default=500)
    parser.add_argument("--profile", choices=("smoke", "reference", "extended"), default="smoke")
    parser.add_argument("--token-env")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--overwrite", action="store_true")
    args = parser.parse_args(argv)
    if args.concurrency < 1 or args.concurrency > 1_000:
        parser.error("--concurrency must be between 1 and 1000")
    if args.requests is not None and not 1 <= args.requests <= 10_000_000:
        parser.error("--requests must be between 1 and 10000000")
    if args.duration_seconds is not None and not 0 < args.duration_seconds <= 604_800:
        parser.error("--duration-seconds must be greater than 0 and at most 604800")
    if args.requests_per_second <= 0 or args.timeout_seconds <= 0 or args.maximum_p95_ms <= 0:
        parser.error("rate, timeout, and latency threshold must be greater than zero")
    try:
        target = validate_target(args.base_url, args.path)
        token = None
        if args.token_env is not None:
            token = os.environ.get(args.token_env)
            if not token:
                raise ValueError(f"token environment variable is unset or empty: {args.token_env}")
        started_at = datetime.now(UTC)
        started = time.perf_counter()
        if args.requests is not None:
            samples = run_request_count(
                target,
                request_count=args.requests,
                concurrency=args.concurrency,
                token=token,
                timeout_seconds=args.timeout_seconds,
            )
        else:
            samples = run_duration(
                target,
                duration_seconds=args.duration_seconds,
                concurrency=args.concurrency,
                requests_per_second=args.requests_per_second,
                token=token,
                timeout_seconds=args.timeout_seconds,
            )
        payload = summarize(
            samples,
            profile=args.profile,
            public_target=target,
            started_at=started_at,
            duration_seconds=time.perf_counter() - started,
            concurrency=args.concurrency,
            maximum_p95_ms=args.maximum_p95_ms,
        )
        payload["samples"] = [asdict(sample) for sample in samples[:100]]
        payload["samples_truncated"] = len(samples) > 100
        _write_json(args.output, payload, overwrite=args.overwrite)
    except (OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(json.dumps({key: payload[key] for key in ("status", "requests", "failures")}))
    return 0 if payload["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
