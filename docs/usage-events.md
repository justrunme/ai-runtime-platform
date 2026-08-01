# Usage events

Runtime emits chargeback-quality usage events. Control Plane aggregates; Runtime
does not store a billing ledger.

## Event shape

```json
{
  "event_id": "evt_...",
  "tenant_id": "finance",
  "request_id": "req_...",
  "decision_id": "dec_...",
  "model": "qwen",
  "backend": "qwen",
  "input_tokens": 1200,
  "output_tokens": 350,
  "estimated_cost_usd": 0.0042,
  "gpu_seconds": 2.8,
  "ttft_ms": null,
  "duration_ms": 1830,
  "outcome": "success"
}
```

## Delivery

1. Always attach fields to the active OTEL span (`ai.runtime.usage.*`).
2. Optionally POST to `USAGE_EVENTS_WEBHOOK_URL` with:
   - `Idempotency-Key: <event_id>`
   - bounded local buffer (`USAGE_EVENTS_BUFFER_MAX`, default 1000)
   - drop metrics on overflow
