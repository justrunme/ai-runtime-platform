#!/usr/bin/env python3
"""Recommend promote, hold, or rollback from primary vs canary shadow metrics."""

from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path

REQUIRED_COLUMNS = {
    "primary_model",
    "canary_model",
    "sample_requests",
    "primary_p95_latency_ms",
    "canary_p95_latency_ms",
    "primary_error_rate",
    "canary_error_rate",
    "primary_cost_usd",
    "canary_cost_usd",
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Recommend canary promotion from shadow comparison metrics."
    )
    parser.add_argument(
        "--input",
        type=Path,
        default=Path(__file__).with_name("sample_metrics.csv"),
        help="CSV with primary and canary comparison metrics.",
    )
    parser.add_argument(
        "--max-latency-regression-pct",
        type=float,
        default=10.0,
        help="Maximum acceptable canary p95 latency increase versus primary.",
    )
    parser.add_argument(
        "--max-error-regression-pct",
        type=float,
        default=5.0,
        help="Maximum acceptable canary error-rate increase versus primary.",
    )
    parser.add_argument(
        "--max-cost-regression-pct",
        type=float,
        default=15.0,
        help="Maximum acceptable canary cost increase versus primary.",
    )
    parser.add_argument(
        "--min-latency-improvement-pct",
        type=float,
        default=5.0,
        help="Latency improvement required to auto-promote when errors and cost are flat.",
    )
    parser.add_argument("--output", type=Path, help="Optional JSON output path.")
    return parser.parse_args()


def load_rows(path: Path) -> list[dict[str, float | str]]:
    if not path.exists():
        raise FileNotFoundError(f"input file does not exist: {path}")

    rows: list[dict[str, float | str]] = []
    with path.open(newline="") as csv_file:
        reader = csv.DictReader(csv_file)
        missing = REQUIRED_COLUMNS - set(reader.fieldnames or [])
        if missing:
            raise ValueError(f"missing required columns: {', '.join(sorted(missing))}")

        for row in reader:
            rows.append(
                {
                    "primary_model": row["primary_model"],
                    "canary_model": row["canary_model"],
                    "sample_requests": float(row["sample_requests"]),
                    "primary_p95_latency_ms": float(row["primary_p95_latency_ms"]),
                    "canary_p95_latency_ms": float(row["canary_p95_latency_ms"]),
                    "primary_error_rate": float(row["primary_error_rate"]),
                    "canary_error_rate": float(row["canary_error_rate"]),
                    "primary_cost_usd": float(row["primary_cost_usd"]),
                    "canary_cost_usd": float(row["canary_cost_usd"]),
                }
            )
    return rows


def percent_delta(primary: float, canary: float) -> float:
    if primary == 0:
        return 0.0 if canary == 0 else 100.0
    return round(((canary - primary) / primary) * 100, 2)


def recommend_verdict(
    row: dict[str, float | str],
    *,
    max_latency_regression_pct: float,
    max_error_regression_pct: float,
    max_cost_regression_pct: float,
    min_latency_improvement_pct: float,
) -> dict[str, object]:
    latency_delta = percent_delta(
        float(row["primary_p95_latency_ms"]), float(row["canary_p95_latency_ms"])
    )
    error_delta = percent_delta(float(row["primary_error_rate"]), float(row["canary_error_rate"]))
    cost_delta = percent_delta(float(row["primary_cost_usd"]), float(row["canary_cost_usd"]))

    reasons: list[str] = []
    if latency_delta > max_latency_regression_pct:
        reasons.append(f"canary p95 latency +{latency_delta}%")
    if error_delta > max_error_regression_pct:
        reasons.append(f"canary error rate +{error_delta}%")
    if cost_delta > max_cost_regression_pct:
        reasons.append(f"canary cost +{cost_delta}%")

    if reasons:
        recommendation = "rollback" if latency_delta > 0 or error_delta > 0 else "hold"
        promote = False
        reason = ", ".join(reasons)
    elif (
        latency_delta <= -min_latency_improvement_pct
        and error_delta <= 0
        and cost_delta <= max_cost_regression_pct
    ):
        recommendation = "promote"
        promote = True
        reason = (
            f"canary p95 latency {latency_delta}%, error rate {error_delta}%, cost {cost_delta}%"
        )
    else:
        recommendation = "hold"
        promote = False
        reason = (
            "canary is within guardrails but does not beat primary enough to promote "
            f"(latency {latency_delta}%, error {error_delta}%, cost {cost_delta}%)"
        )

    return {
        "primary_model": row["primary_model"],
        "canary_model": row["canary_model"],
        "sample_requests": row["sample_requests"],
        "promote": promote,
        "recommendation": recommendation,
        "reason": reason,
        "deltas": {
            "p95_latency_pct": latency_delta,
            "error_rate_pct": error_delta,
            "cost_pct": cost_delta,
        },
        "signals": {
            "primary_p95_latency_ms": row["primary_p95_latency_ms"],
            "canary_p95_latency_ms": row["canary_p95_latency_ms"],
            "primary_error_rate": row["primary_error_rate"],
            "canary_error_rate": row["canary_error_rate"],
            "primary_cost_usd": row["primary_cost_usd"],
            "canary_cost_usd": row["canary_cost_usd"],
        },
    }


def build_result(
    path: Path,
    *,
    max_latency_regression_pct: float,
    max_error_regression_pct: float,
    max_cost_regression_pct: float,
    min_latency_improvement_pct: float,
) -> dict[str, object]:
    rows = load_rows(path)
    analyses = [
        recommend_verdict(
            row,
            max_latency_regression_pct=max_latency_regression_pct,
            max_error_regression_pct=max_error_regression_pct,
            max_cost_regression_pct=max_cost_regression_pct,
            min_latency_improvement_pct=min_latency_improvement_pct,
        )
        for row in rows
    ]
    counts = {
        recommendation: sum(1 for item in analyses if item["recommendation"] == recommendation)
        for recommendation in ("promote", "hold", "rollback")
    }
    return {
        "input_source": str(path),
        "thresholds": {
            "max_latency_regression_pct": max_latency_regression_pct,
            "max_error_regression_pct": max_error_regression_pct,
            "max_cost_regression_pct": max_cost_regression_pct,
            "min_latency_improvement_pct": min_latency_improvement_pct,
        },
        "recommendation_counts": counts,
        "analyses": analyses,
    }


def main() -> int:
    args = parse_args()
    result = build_result(
        args.input,
        max_latency_regression_pct=args.max_latency_regression_pct,
        max_error_regression_pct=args.max_error_regression_pct,
        max_cost_regression_pct=args.max_cost_regression_pct,
        min_latency_improvement_pct=args.min_latency_improvement_pct,
    )
    encoded = json.dumps(result, indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(f"{encoded}\n")
    else:
        print(encoded)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
