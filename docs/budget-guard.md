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

Spend is tracked per scope (`tenant:<id>` or `key:<tenant>:<sha256(apiKey)[:16]>`; the
raw key never appears in Redis key names, metric labels or error bodies) inside a
fixed window:

| Field | Meaning |
| --- | --- |
| `committed_micros` | Settled actual spend in the active window |
| `reserved_micros` | In-flight reservations not yet settled |
| reservation ZSET | `reservation_id → expiry`; TTL reclaim on expiry |
| amounts hash | `reservation_id → micros` while live, `x<micros>` tombstone after expiry |

All money is **integer micro-USD**. Estimates and actual charges round up, the budget
cap rounds down (Decimal arithmetic, no binary float sums).

Window rollover resets `committed_micros` only; in-flight reservations carry over into
the new window, so a rollover cannot admit spend on top of requests still running.

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
| Corrupt store payload (non-numeric window or reservation fields, bad script reply) | 503 | `store_corrupt` |
| Invalid estimate (NaN / infinite / negative) | 503 | `store_corrupt` |
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

`INFERENCE_BUDGET_STORE_TIMEOUT_SECONDS` (default `1.0`) bounds the whole Redis
exchange (script load, eval, NOSCRIPT reload).

The guard is **off by default** (`INFERENCE_BUDGET_ENABLED` unset): no reservation, no
budget headers, no behaviour change. With the guard on and `REDIS_URL` unset, the
in-process backend is used — budgets are then **per gateway process**, not shared
across replicas. Set `REDIS_URL` for a shared budget.

## Settlement

Each reservation is settled exactly once:

| Outcome | Settlement |
| --- | --- |
| Upstream success with a `usage` block | actual priced usage (gateway model-target prices) |
| Upstream success without `usage` (most streams) | charged at the estimate |
| Error before the model is called (governance deny, quota deny, no backend, unknown model, connect refused, upstream HTTP status error) | released (`0`) |
| Error after the model may have run (read timeout, unparsable body, bookkeeping error) | charged at the estimate |
| Settle arrives after the reservation's TTL expired | actual spend still committed (tombstone), once |
| Duplicate settle for the same reservation | no-op |

Actual spend above the estimate is committed as-is (it really happened), so committed
spend can exceed the cap by at most the estimate error of in-flight requests; later
requests are then denied until the window rolls over.

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
- **Settlement can lag** if Redis fails after upstream success: the reservation stays
  held until its TTL and is then released, and that request's actual spend is not
  committed.
- Estimate prices come from the budget policy, settlement prices from gateway model
  targets — keep them aligned.
- Reservation expiry uses each gateway's wall clock; replica clock skew shifts TTL release.
- A client disconnect before a streaming body starts may skip settlement; the TTL
  releases the reservation.
- Atomicity is exercised against a real Redis server with multiple OS processes in CI
  (plus in-process memory and fakeredis unit tests) — one Redis, **not** multi-region
  billing or a chargeback ledger.
- Do not read results as a blanket “€0 overspend” guarantee outside this suite.
