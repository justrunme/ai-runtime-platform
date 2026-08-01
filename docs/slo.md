# Runtime Gateway SLO targets

These are the documented service objectives for the Execution Plane gateway in production profile (`deploy/overlays/production`).

| SLO | Target | Measurement |
| --- | --- | --- |
| Availability | 99.9% successful non-5xx on `/v1/chat/completions` excluding upstream model faults | Gateway metrics + synthetic probes |
| Readiness correctness | Pod not Ready when Redis required but unavailable | `/readyz` |
| Decision durability | Decision readable from any replica after write | Redis decision store |
| Streaming honesty | Success metric only after stream completion | `gateway_stream_outcomes_total` |
| Governance fail-closed | CP unavailable → 503 when `GOVERNANCE_FAIL_OPEN=false` | Contract tests |
| Auth fail-closed | Invalid JWT → 401 when verify enabled | Identity tests |

Upstream model latency/error budgets are owned by the model serving layer (vLLM), not the gateway.
