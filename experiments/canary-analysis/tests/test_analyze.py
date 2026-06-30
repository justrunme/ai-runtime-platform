import importlib.util
from pathlib import Path

ANALYZE_PATH = Path(__file__).resolve().parents[1] / "analyze.py"


def load_analyze():
    spec = importlib.util.spec_from_file_location("canary_analyze", ANALYZE_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_percent_delta_handles_zero_primary() -> None:
    analyze = load_analyze()
    assert analyze.percent_delta(0, 0) == 0.0
    assert analyze.percent_delta(0, 5) == 100.0


def test_recommend_verdict_rollbacks_on_latency_regression() -> None:
    analyze = load_analyze()
    result = analyze.recommend_verdict(
        {
            "primary_model": "qwen",
            "canary_model": "llama",
            "sample_requests": 1000,
            "primary_p95_latency_ms": 400,
            "canary_p95_latency_ms": 532,
            "primary_error_rate": 0.01,
            "canary_error_rate": 0.01,
            "primary_cost_usd": 0.10,
            "canary_cost_usd": 0.11,
        },
        max_latency_regression_pct=10,
        max_error_regression_pct=5,
        max_cost_regression_pct=15,
        min_latency_improvement_pct=5,
    )
    assert result["promote"] is False
    assert result["recommendation"] == "rollback"
    assert "latency +33.0%" in result["reason"]


def test_recommend_verdict_promotes_on_clear_improvement() -> None:
    analyze = load_analyze()
    result = analyze.recommend_verdict(
        {
            "primary_model": "qwen",
            "canary_model": "qwen-v2",
            "sample_requests": 500,
            "primary_p95_latency_ms": 500,
            "canary_p95_latency_ms": 450,
            "primary_error_rate": 0.02,
            "canary_error_rate": 0.015,
            "primary_cost_usd": 0.12,
            "canary_cost_usd": 0.11,
        },
        max_latency_regression_pct=10,
        max_error_regression_pct=5,
        max_cost_regression_pct=15,
        min_latency_improvement_pct=5,
    )
    assert result["promote"] is True
    assert result["recommendation"] == "promote"


def test_build_result_reads_sample_csv() -> None:
    analyze = load_analyze()
    sample = Path(__file__).resolve().parents[1] / "sample_metrics.csv"
    result = analyze.build_result(
        sample,
        max_latency_regression_pct=10,
        max_error_regression_pct=5,
        max_cost_regression_pct=15,
        min_latency_improvement_pct=5,
    )
    assert len(result["analyses"]) == 2
    assert result["recommendation_counts"]["rollback"] >= 1
