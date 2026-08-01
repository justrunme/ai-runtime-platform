# Redis HA reference (external / Sentinel example)

Production Runtime gateways must consume Redis through Secret `ai-runtime-redis` /
`REDIS_URL` (preferably `rediss://` with ACL credentials). This overlay is a
**reference** for operators evaluating Sentinel topology — not a turnkey managed service.

## Contract

| Concern | Expectation |
| --- | --- |
| Topology | Managed Redis, Sentinel, or Cluster |
| Auth | Password / ACL required |
| Transport | TLS (`rediss://`) in production |
| Durability | Operator-defined RPO; gateway keys are TTL operational state, not audit |
| Gateway | `REDIS_URL` via `secretKeyRef` (see `deploy/overlays/production`) |

## Apply (reference only)

```bash
# Review and replace placeholders, then:
kubectl apply -k deploy/overlays/redis-ha
# Point ai-runtime-redis Secret url at the Sentinel-aware endpoint.
```

Prefer a cloud managed Redis (AWS MemoryDB / ElastiCache, GCP Memorystore, Azure Cache)
when you need backup/restore and multi-AZ without operating Sentinel yourself.
