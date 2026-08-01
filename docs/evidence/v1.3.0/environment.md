# Evidence environment template — v1.3.0

Fill this file when attaching measured SLO evidence for a release.

| Field | Value |
| --- | --- |
| Date | |
| Runner | |
| CPU / RAM | |
| Concurrency | |
| Duration | |
| Gateway image | `ghcr.io/justrunme/ai-runtime-platform:1.3.0` |
| Control Plane image | `ghcr.io/justrunme/ai-infra-control-plane:1.3.0` |
| Redis | |
| Model / mock | mock OpenAI or named GPU model |
| Auth mode | OIDC production profile / internal API key |

Commands:

```bash
python scripts/slo_benchmark.py --base-url http://127.0.0.1:8080 --requests 200 --output docs/evidence/v1.3.0/benchmark.json
k6 run -e BASE_URL=http://127.0.0.1:8080 benchmarks/k6/chat.js
./deploy/e2e/run_e2e_real_cp.sh
```
