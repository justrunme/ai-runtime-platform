# Runtime Gateway SLO targets

These are **SLO targets** for the Execution Plane gateway in the production profile
(`deploy/overlays/production`). Until a release includes measured evidence under
`docs/evidence/`, treat the availability number as a design target, not a proven result.

| SLO | Target | Measurement |
| --- | --- | --- |
| Availability | 99.9% successful non-5xx on `/v1/chat/completions` excluding upstream model faults | Gateway metrics + synthetic probes |
| Readiness correctness | Pod not Ready when Redis required but unavailable | `/readyz` |
| Decision durability | Decision readable from any replica after write (TTL-bound, not audit log) | Redis decision store |
| Streaming honesty | Success metric only after stream completion; interruptions classified | `gateway_stream_outcomes_total` |
| Governance fail-closed | CP unavailable → 503 when `GOVERNANCE_FAIL_OPEN=false` | Contract + platform e2e |
| Auth fail-closed | Invalid JWT → 401; production profile requires OIDC/JWKS | Identity + profile tests |

Upstream model latency/error budgets are owned by the model serving layer (vLLM), not the gateway.

## Benchmark harness

Use the local harness for release evidence (not a substitute for production telemetry):

```bash
python scripts/slo_benchmark.py --base-url http://127.0.0.1:8080 --requests 200
```

Optional k6 script: `benchmarks/k6/chat.js`.

Capture and attach under `docs/evidence/<release>/` when publishing measured results:

- latency p50/p95/p99
- TTFT p50/p95/p99 (streaming)
- max concurrent streams
- Redis restart / gateway pod termination during stream
- slow Control Plane / slow model backend
- memory and connection growth notes
