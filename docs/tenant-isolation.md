# Tenant isolation

Hard boundary on the Execution Plane for decisions, quotas, and admission.

## Tenant identity

When `OIDC_JWT_VERIFY` is enabled, tenant is taken only from JWT claims:

1. `tenant_id`
2. `tenant`
3. `team`
4. else `user:{sub}`

Client headers such as `x-ai-tenant` are ignored for isolation decisions.

## Decision ACL

- Records include `tenant_id`
- Redis: `arp:{tenant_id}:decision:{request_id}` (+ index for auditors)
- `GET /v1/decisions/{request_id}` returns 404 across tenants
- Groups in `auditorGroups` (default `ai-auditors`, `global-auditor`) may read any tenant

## Runtime policy

```bash
TENANT_RUNTIME_POLICY='{
  "tenants": {
    "finance": {
      "allowedModels": ["qwen"],
      "allowedRoutes": ["finance-chat"],
      "maxConcurrentRequests": 20,
      "maxQueuedRequests": 100,
      "redisUnavailablePolicy": "fail_closed"
    }
  },
  "auditorGroups": ["ai-auditors"]
}'
```

Empty allowlists mean unrestricted (default demo posture).

### Fleet-wide concurrency admission

When `REDIS_URL` is set, `maxConcurrentRequests` is enforced **fleet-wide** with a
Redis-backed distributed semaphore keyed by tenant and model:

- Atomic acquire via Lua (purge expired leases, check cardinality, fence token)
- Each hold is a lease with TTL (`ADMISSION_LEASE_TTL_SECONDS`, default `30`)
- Leases renew while the request/stream is active (`ADMISSION_LEASE_RENEW_INTERVAL_SECONDS`)
- Release on completion or cancel; fencing token prevents stale releases
- Pod death recovers automatically when the lease TTL expires

`maxQueuedRequests` bounds **per-replica waiters** for a fleet slot (process-local queue).

Legacy keys `maxConcurrentRequestsPerReplica` / `maxQueuedRequestsPerReplica` remain
accepted as aliases.

Without Redis, limits stay process-local (single-replica / demo posture).

### Redis unavailable policy

Per tenant (`redisUnavailablePolicy`), or default via
`ADMISSION_REDIS_UNAVAILABLE_POLICY`:

| Policy | Behaviour |
| --- | --- |
| `fail_closed` (default) | `503` `admission_store_unavailable` |
| `degraded` | Fall back to process-local semaphore |
| `fail_open` | Admit without a concurrency hold |

Status is exposed on `/readyz` (`checks.admission`) and `/v1/runtime/status`
(`admission`). Metrics: `gateway_admission_inflight`,
`gateway_admission_acquire_seconds`, `gateway_admission_rejections_total`,
`gateway_admission_leases_expired_total`.

`upstreamCredentialRef` is reserved and not applied to upstream Authorization yet.

## Admission errors

Concurrency overflow:

```json
{
  "error": {
    "type": "rate_limit_error",
    "code": "tenant_concurrency_exceeded",
    "message": "tenant concurrent request limit exceeded"
  }
}
```

Headers: `Retry-After`, `X-RateLimit-Limit`, `X-RateLimit-Remaining`.

Fleet store unavailable (fail-closed):

```json
{
  "error": {
    "type": "api_error",
    "code": "admission_store_unavailable",
    "message": "fleet admission store unavailable; tenant policy is fail-closed for Redis outages"
  }
}
```
