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

## Layering

```text
drain check
  → global admission
  → tenant admission / allowlist
  → Control Plane governance
  → circuit-aware routing
  → inference (+ retry budget on fallback)
```
