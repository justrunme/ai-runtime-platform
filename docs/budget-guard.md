# Inference Budget Guard

Admission control that **reserves estimated inference cost** before a chat completion
runs, and **settles** against actual priced usage afterwards. Integrated into the
gateway request path beside fleet concurrency admission — not a standalone sidecar.

## Request path

```text
drain check
  → global admission
  → tenant / fleet concurrency admission
  → inference budget reserve   ← this document
  → tenant attribution pre-reserve (signals)
  → Control Plane governance
  → route / execute
  → settle budget + reconcile attribution
```

On `DENY`, the gateway returns `429` (`inference_budget_exceeded`) or `503`
(`budget_store_*` / `budget_policy_invalid`) and does not call the model backend.

## Atomic reservation

Spend is tracked per scope (`tenant:<id>` or `key:<tenant>:<apiKey>`) inside a
sliding window:

| Field | Meaning |
| --- | --- |
| `committed_micros` | Settled actual spend in the active window |
| `reserved_micros` | In-flight reservations not yet settled |
| reservation ZSET | `reservation_id → expiry`; TTL reclaim on expiry |

Invariant enforced atomically (Redis Lua or in-process lock):

```text
committed + reserved + estimate ≤ budget
```

Redis script keys: `arp:budget:{scope}:window|reservations|amounts`.

## Fail-closed

| Condition | HTTP | Reason |
| --- | --- | --- |
| Would exceed budget | 429 | `budget_exceeded` |
| Store unavailable | 503 | `store_unavailable` |
| Store timeout | 503 | `store_timeout` |
| Corrupt store payload | 503 | `store_corrupt` |
| Malformed / missing policy while enabled | 503 | `malformed_policy` |

There is **no silent allow** when the guard is enabled and the store or policy fails.

## Policy

Enable with `INFERENCE_BUDGET_ENABLED=true` and either:

- `INFERENCE_BUDGET_POLICY` — inline JSON, or
- `INFERENCE_BUDGET_POLICY_FILE` — path to JSON

Example: [budget-guard.policy.json](budget-guard.policy.json).

| Field | Role |
| --- | --- |
| `budgetUsd` | Hard cap for the window |
| `windowSeconds` | Spend window length |
| `softWarningRatio` | Fraction of budget that sets `X-AI-Budget-Warning: soft_threshold` |
| `reservationTtlSeconds` | Abandoned reservations are released after TTL |
| `prices` | Token × price table (`*` / `default` required) |
| `tenants[].apiKeys` | Optional per-key budgets |

`INFERENCE_BUDGET_STORE_TIMEOUT_SECONDS` (default `1.0`) bounds Redis calls.

## Settlement

After upstream success, the reservation is settled with actual priced USD (from
usage × model unit prices). Unused estimate is released from `reserved_micros`.
On governance deny / upstream failure / missing model, the reservation is released
at `0`.

## Evidence

Each decision can be emitted as an ASC-shaped envelope (`schema_version` 1.0.0)
with `decision` = `ALLOW` \| `DENY` (plus `verdict` / `acs_disposition` for
compatibility with [agent-security-contract](https://github.com/justrunme/agent-security-contract)).

CI regenerates [results.json](results.json) and [EVIDENCE.md](EVIDENCE.md) via:

```bash
python scripts/generate_budget_evidence.py
```

## Honest limits (on this setup)

- Claims are **on this test suite / this CI setup** only.
- Pre-request **estimate can differ** from actual token usage.
- **Settlement can lag** if Redis fails after upstream success; TTL still releases
  abandoned reservations.
- Fakeredis / in-process memory prove atomicity here — **not** multi-region
  billing or a chargeback ledger.
- Do not read results as a blanket “€0 overspend” guarantee outside this suite.
