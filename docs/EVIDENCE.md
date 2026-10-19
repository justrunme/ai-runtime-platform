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
| Fleet-wide Redis admission (lease fencing) | Tested in CI | PR [#98](https://github.com/justrunme/ai-runtime-platform/pull/98): `test_fleet_admission` (fakeredis atomicity/fencing/expiry/unavailable policies) + **platform-e2e** / **platform-e2e-oidc** with `deploy/e2e/docker-compose.admission.yaml` (2 replicas share global limit; SIGKILL mid-stream recovers after TTL; Redis down → `503 admission_store_unavailable`) | Compose Redis on CI runner — not multi-region global admission | Main CI after #98: [38121712037](https://github.com/justrunme/ai-runtime-platform/actions/runs/38121712037) · SHA `0bbc9165d1a212e9e1dba8c510eccac1b7694d6c` · jobs platform-e2e / platform-e2e-oidc / python | Acquire must be atomic; expired leases free capacity; fail_closed default when Redis unavailable |
| Billing ledger / automated canary promotion | Not yet | Called out in maturity docs | Out of Supported scope | — | — |
| Inference budget guard: atomic reserve ≤ budget under concurrency | Tested in CI | `test_budget_guard` · 40 parallel × 25 rounds × memory+redis/fakeredis | On this test suite / fakeredis or in-process memory — not multi-region billing; estimate may differ from actual usage; settlement can lag | (local) · SHA `307943d1fe5615c6695e3c121a5dc8e45f0942a1` | overspend_events=0; max_total_usd_observed=0.1 ≤ budget 0.1 |
| Inference budget guard: fail-closed on store down/timeout/corrupt + malformed policy | Tested in CI | Same suite | Denies with 503; no silent allow on this setup | (local) · SHA `307943d1fe5615c6695e3c121a5dc8e45f0942a1` | store_down, store_timeout, store_corrupt, malformed_policy |
| Inference budget guard: over-budget deny, settlement refund, expired release | Tested in CI | Same suite | Reservation TTL reclaim; refund = reserved − actual | (local) · SHA `307943d1fe5615c6695e3c121a5dc8e45f0942a1` | over_budget, settlement_refunds_unused, expired_reservation |
| Inference budget guard: legitimate within-budget allow (false-deny=0) | Tested in CI | Same suite | On this test suite only | (local) · SHA `307943d1fe5615c6695e3c121a5dc8e45f0942a1` | false_deny_count_legitimate |

## Inference budget guard (this suite)

- Scope: **on this test suite**
- Policy version: `budget-guard-1.0`
- Policy sha256: `sha256:eff951135b69d4ff1a86da7f40775c60b3459a3462d46f4e88674d9a11e10d6c`
- Scenario envelopes: `14`
- False-deny count (legitimate): `0`
- Concurrency overspend events: `0` (max total USD observed `0.1` vs budget `0.1`)

### Decision envelopes

| Scenario | Decision | Reason |
| --- | --- | --- |
| `concurrency_atomic_memory` | ALLOW | `concurrency_invariant_held` |
| `concurrency_atomic_redis` | ALLOW | `concurrency_invariant_held` |
| `expired_reservation_released` | ALLOW | `within_budget` |
| `legitimate_within_budget` | ALLOW | `within_budget` |
| `malformed_policy_fail_closed` | DENY | `malformed_policy` |
| `over_budget_denied` | DENY | `budget_exceeded` |
| `settlement_refunds_unused` | ALLOW | `within_budget` |
| `store_corrupt_fail_closed` | DENY | `store_corrupt` |
| `store_down_fail_closed` | DENY | `store_unavailable` |
| `store_timeout_fail_closed` | DENY | `store_timeout` |

### Honest limits

- Claims apply on this test suite / this setup only.
- Pre-request estimate can differ from actual token usage.
- Settlement can lag if the store is unavailable after upstream success; expired reservations are released by TTL.
- In-memory backend proves single-process atomicity; Redis/fakeredis proves Lua atomicity on this CI setup — not multi-region billing.

## Not proven / out of scope

- Multi-region gateway mesh / cross-region global admission (fleet Redis admission is compose/CI-proved only)
- Production GPU capacity planning (vLLM chart is a template)
- Billing / chargeback ledger (budget guard is an admission control, not an invoice system)
- Treating compose golden-path seconds as production p99 latency
- Blanket €0 overspend outside this test suite

## How to reproduce

```bash
make validate
python scripts/generate_budget_evidence.py
# golden paths (see docs/demo-flow.md and deploy/e2e/):
bash deploy/e2e/run_e2e.sh
```

CI entrypoints: `.github/workflows/ci.yaml`, `.github/workflows/budget-guard.yml`.
