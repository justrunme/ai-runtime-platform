# Results

Measured from CI run [`38060436977`](https://github.com/justrunme/ai-runtime-platform/actions/runs/38060436977) (branch `main`, job **platform-e2e**, 2026-10-10).

Script: [`deploy/e2e/run_e2e.sh`](../deploy/e2e/run_e2e.sh) against `deploy/e2e/docker-compose.yaml`.

## Golden-path outcomes

| Step | HTTP / outcome | Notes |
| --- | --- | --- |
| Allow → inference | 200 | Mock backend `chat_completions` incremented |
| Block | **403** | Backend not called; `error.code=governance_blocked` |
| Approval required | **409** then approve → retry 200 | Approval after CP approve |
| Approval replay | **409** | Replay rejected |
| Approval body mismatch | **409** | Digest mismatch rejected |
| Streaming | `[DONE]` | Stream completed |
| Cross-replica decision | OK | Decision readable from other gateway |
| Gateway-A stop | traffic via gateway-B | HA path |
| Redis unavailable | `/readyz` **503** | Readiness fail-closed |
| Control Plane unavailable | **503** | Fail-closed |

## Timing (job log wall clock)

| Metric | Value | Source |
| --- | --- | --- |
| Golden-path suite (after compose up) | **21.5 s** | 14:40:46.750Z (`wait for readiness`) → 14:41:08.280Z (`OK: platform e2e golden paths passed`) |

Job log: [platform-e2e](https://github.com/justrunme/ai-runtime-platform/actions/runs/38060436977/job/114237651314).
