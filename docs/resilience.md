# Resilience and admission control

## Global capacity

| Env | Default | Meaning |
| --- | --- | --- |
| `GATEWAY_MAX_INFLIGHT` | `256` | Max concurrent chat requests per process |
| `GATEWAY_MAX_QUEUED` | `256` | Max waiters when inflight is saturated |
| `GATEWAY_CIRCUIT_FAILURE_THRESHOLD` | `5` | Consecutive upstream failures before open |
| `GATEWAY_CIRCUIT_RECOVERY_SECONDS` | `30` | Cool-down before half-open probe |
| `GATEWAY_RETRY_BUDGET_PER_MINUTE` | `60` | Fallback retries allowed per minute |

Load shed response:

```json
{
  "error": {
    "type": "rate_limit_error",
    "code": "gateway_load_shed",
    "message": "gateway capacity exceeded; request shed"
  }
}
```

## Graceful drain

On `SIGTERM` / `SIGINT` (and during lifespan shutdown):

1. `DrainState` becomes active
2. `/readyz` returns `503` with `status=draining` (pods leave Service endpoints)
3. New `/v1/chat/completions` requests return `503 gateway_draining`
4. In-flight streams finish and release admission leases

## Tenant / fleet admission

| Env | Default | Meaning |
| --- | --- | --- |
| `REDIS_URL` | unset | When set, tenant concurrency is fleet-wide |
| `ADMISSION_LEASE_TTL_SECONDS` | `30` | Lease lifetime; expiry reclaims capacity after pod loss |
| `ADMISSION_LEASE_RENEW_INTERVAL_SECONDS` | `10` | Renewal cadence while a request/stream holds a lease |
| `ADMISSION_REDIS_UNAVAILABLE_POLICY` | `fail_closed` | Default when tenant policy omits `redisUnavailablePolicy` |

See [tenant isolation](tenant-isolation.md) for semaphore semantics, fencing tokens,
and per-tenant fail-closed / degraded / fail-open behaviour.

## Layering

```text
drain check
  → global admission (process-local)
  → tenant / fleet admission (+ allowlist)
  → Control Plane governance
  → circuit-aware routing
  → inference (+ retry budget on fallback)
```

