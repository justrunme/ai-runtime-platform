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
2. Optionally enqueue for webhook delivery (`USAGE_EVENTS_WEBHOOK_URL`):
   - `Idempotency-Key` / `X-Event-Id` = deterministic `event_id`
   - background worker with exponential backoff (`USAGE_EVENTS_MAX_ATTEMPTS`)
   - bounded buffer (`USAGE_EVENTS_BUFFER_MAX`)
   - metrics: buffer depth, delivery lag, drops (`buffer_overflow` / `max_attempts` / `shutdown_timeout`)
   - on gateway shutdown: stop accept → drain until timeout

## Streaming

When the upstream SSE includes a final `usage` object (OpenAI `stream_options.include_usage`),
Runtime records tokens/cost/TTFT/outcome on the stream usage event.
