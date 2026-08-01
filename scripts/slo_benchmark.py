#!/usr/bin/env python3
"""Lightweight chat-completions latency harness for SLO target evidence."""

from __future__ import annotations

import argparse
import json
import statistics
import time
import urllib.error
import urllib.request


def percentile(values: list[float], pct: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, round((pct / 100) * (len(ordered) - 1))))
    return ordered[index]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:8080")
    parser.add_argument("--model", default="qwen")
    parser.add_argument("--requests", type=int, default=50)
    parser.add_argument("--api-key", default="")
    parser.add_argument("--output", default="")
    args = parser.parse_args()

    latencies: list[float] = []
    errors = 0
    payload = json.dumps(
        {"model": args.model, "messages": [{"role": "user", "content": "ping"}]}
    ).encode()
    headers = {"Content-Type": "application/json"}
    if args.api_key:
        headers["Authorization"] = f"Bearer {args.api_key}"

    for _ in range(args.requests):
        request = urllib.request.Request(
            f"{args.base_url.rstrip('/')}/v1/chat/completions",
            data=payload,
            headers=headers,
            method="POST",
        )
        started = time.perf_counter()
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                response.read()
                if response.status >= 500:
                    errors += 1
                else:
                    latencies.append((time.perf_counter() - started) * 1000)
        except (urllib.error.URLError, TimeoutError):
            errors += 1

    report = {
        "requests": args.requests,
        "successes": len(latencies),
        "errors": errors,
        "latency_ms": {
            "p50": round(percentile(latencies, 50), 2),
            "p95": round(percentile(latencies, 95), 2),
            "p99": round(percentile(latencies, 99), 2),
            "mean": round(statistics.fmean(latencies), 2) if latencies else 0.0,
        },
        "note": "SLO targets until attached under docs/evidence/<release>/",
    }
    text = json.dumps(report, indent=2)
    print(text)
    if args.output:
        with open(args.output, "w", encoding="utf-8") as handle:
            handle.write(text + "\n")
    return 0 if latencies else 1


if __name__ == "__main__":
    raise SystemExit(main())
