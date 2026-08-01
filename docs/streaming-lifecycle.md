# Streaming lifecycle

Streaming chat completions are observed end-to-end. Success is recorded only after the body finishes, not when upstream response headers arrive.

| Outcome | Meaning |
| --- | --- |
| `success` | Stream closed cleanly with `[DONE]` or non-empty OpenAI-compatible EOF |
| `empty_stream` | Stream ended with zero bytes and no error |
| `client_disconnected` | Client cancelled mid-stream (`CancelledError` / `GeneratorExit`) |
| `upstream_error_before_first_byte` | Upstream failed before any bytes were sent |
| `upstream_interrupted` | Upstream failed after at least one byte was sent |

Metrics:

- `gateway_stream_outcomes_total{outcome,selected_backend}`
- `gateway_stream_ttft_seconds{selected_backend}`
- `gateway_stream_duration_seconds{selected_backend,outcome}`

Decision records include `stream_outcome` and `stream_ttft_ms`. Response header `x-ai-stream-observed: true` marks the observed path.
