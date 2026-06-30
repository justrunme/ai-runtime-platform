# Canary promotion analysis

Offline simulator that compares primary and canary shadow metrics and recommends **promote**, **hold**, or **rollback**. This mirrors an Argo Rollouts analysis step for LLM inference.

## Input

CSV with one row per route comparison:

| Column | Meaning |
| --- | --- |
| `primary_model` | Stable backend |
| `canary_model` | Candidate backend |
| `sample_requests` | Requests in the analysis window |
| `primary_p95_latency_ms` | Primary p95 latency |
| `canary_p95_latency_ms` | Canary p95 latency |
| `primary_error_rate` | Primary error ratio |
| `canary_error_rate` | Canary error ratio |
| `primary_cost_usd` | Primary estimated cost per request |
| `canary_cost_usd` | Canary estimated cost per request |

Collect signals from gateway shadow traffic (`gateway_chat_shadow_*`), decision records (`GET /v1/decisions/{request_id}`), or Prometheus exports.

## Run

```bash
python experiments/canary-analysis/analyze.py \
  --input experiments/canary-analysis/sample_metrics.csv \
  --output /tmp/canary-report.json
```

Example output:

```json
{
  "promote": false,
  "recommendation": "rollback",
  "reason": "canary p95 latency +32.38%, cost +17.86%"
}
```

## Defaults

| Threshold | Default |
| --- | --- |
| Max latency regression | 10% |
| Max error regression | 5% |
| Max cost regression | 15% |
| Min latency improvement to promote | 5% |

## Flow

```text
shadow metrics CSV
  -> compare primary vs canary
  -> latency / error / cost score
  -> promote | hold | rollback
```
