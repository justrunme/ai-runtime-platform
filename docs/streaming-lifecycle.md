# Streaming lifecycle

Streaming chat completions are observed end-to-end. Success is recorded only after the body finishes (or `[DONE]`), not when upstream response headers arrive.

| Outcome | Meaning |
| --- | --- |
| `success` | Bytes delivered and stream closed cleanly |
| `stream_interrupted` | Stream ended without usable payload |
| `client_disconnected` | Client cancelled mid-stream |
| `upstream_error` | Upstream failed before useful bytes |

Metrics:

- `gateway_stream_outcomes_total{outcome,selected_backend}`
- `gateway_stream_ttft_seconds{selected_backend}`
- `gateway_stream_duration_seconds{selected_backend,outcome}`

Decision records include `stream_outcome` and `stream_ttft_ms`. Response header `x-ai-stream-observed: true` marks the observed path.
