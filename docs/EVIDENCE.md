# Evidence

Maps execution-plane claims to CI proof. Status vocabulary: **Tested in CI** /
**Reference implementation** / **Simulated** / **Not yet**.

Numbers are copied only from [docs/results.md](results.md), [docs/results.json](results.json),
and [docs/compatibility.md](compatibility.md) plus the linked runs. No invented timings.

## Claim table

| Claim | Status | Scenario + environment | Real limits | Proof link | Negative tests |
| --- | --- | --- | --- | --- | --- |
| Golden-path gateway: allow → inference 200 | Tested in CI | Job **platform-e2e**; `deploy/e2e/run_e2e.sh` + `deploy/e2e/docker-compose.yaml` (compose stack, mock/backends as wired in that script) | Compose on CI runner — not a multi-AZ GPU cluster; mock backends for inference path in this job | Results run [38060436977](https://github.com/justrunme/ai-runtime-platform/actions/runs/38060436977) · SHA `911d0b37075306a31225b2105fcff659731dd8da` · job [platform-e2e](https://github.com/justrunme/ai-runtime-platform/actions/runs/38060436977/job/114237651314); suite ~21.5 s after readiness. Latest green CI: [38118081980](https://github.com/justrunme/ai-runtime-platform/actions/runs/38118081980) · SHA `8a2a3e512c7ed71c4f28a89fc12cef2c1c89cece` | Backend must receive allow traffic |
| Governance block returns 403 (backend not called) | Tested in CI | Same platform-e2e golden path | Depends on Control Plane decision wiring in the e2e stack | Same results run / job | Block must not increment mock `chat_completions` |
| Approval required → 409; approve → retry 200; replay/body mismatch → 409 | Tested in CI | Same platform-e2e | Single-use / digest-bound approvals as implemented in gateway tests | Same results run | Replay and body mismatch must stay 409 |
| Fail-closed readiness: Redis down → `/readyz` 503; Control Plane down → 503 | Tested in CI | Same platform-e2e | Process/compose failure injection, not chaos mesh | Same results run | Readiness must not stay 200 when deps are down |
| HA: gateway-A stop, traffic via gateway-B; cross-replica decision | Tested in CI | Same platform-e2e HA steps | Two gateway processes in compose — not multi-region | Same results run | Decision readable across replicas; traffic survives A stop |
| OIDC platform e2e | Tested in CI | Job **platform-e2e-oidc** on main CI | Demo OIDC — not enterprise IdP certification | [38118081980](https://github.com/justrunme/ai-runtime-platform/actions/runs/38118081980) | Auth failures fail the job |
| Signed Control Plane compatibility matrix (cosign verify + e2e) | Tested in CI | Real Control Plane e2e workflow; matrix in `docs/compatibility.md` | Matrix pins listed CP tags/digests at last matrix update; Runtime line shown as 2.3.x in that file even when Runtime release tag moves — read the matrix file for exact pins | [Run 38053041482](https://github.com/justrunme/ai-runtime-platform/actions/runs/38053041482) · digests in [docs/compatibility.md](compatibility.md) (e.g. CP 2.4.0 `sha256:608f7758bd29979c81db146b9d76b83b6a7b906446d31c15dddbabd8c0a13274`) | Signature or e2e fail → matrix row fails |
| vLLM / KServe / KEDA production serving at scale | Reference implementation | Helm chart + example manifests (`charts/vllm-runtime`, `deploy/kserve`, `deploy/keda`) | Chart defaults are a starting profile; no GPU soak numbers in `docs/results.*` | OCI chart publish covered in release path; no kind GPU RPS claims here | — |
| Billing ledger / fleet-wide concurrency / automated canary promotion | Not yet | Called out in maturity docs | Out of Supported scope | — | — |

## Not proven / out of scope

- Multi-region gateway mesh and global admission
- Production GPU capacity planning (vLLM chart is a template)
- Billing / chargeback ledger
- Treating compose golden-path seconds as production p99 latency

## How to reproduce

```bash
make validate
# golden paths (see docs/demo-flow.md and deploy/e2e/):
bash deploy/e2e/run_e2e.sh
```

CI entrypoints: `.github/workflows/ci.yaml` (`platform-e2e`, `platform-e2e-oidc`).
