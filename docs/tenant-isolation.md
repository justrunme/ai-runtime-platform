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
      "maxConcurrentRequestsPerReplica": 20,
      "maxQueuedRequestsPerReplica": 100
    }
  },
  "auditorGroups": ["ai-auditors"]
}'
```

Empty allowlists mean unrestricted (default demo posture).

Limits are **per gateway replica** (process-local). Two replicas with
`maxConcurrentRequestsPerReplica: 20` can admit up to 40 concurrent requests
fleet-wide until a Redis-backed semaphore exists.

`upstreamCredentialRef` is reserved and not applied to upstream Authorization yet.

## Admission errors

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
