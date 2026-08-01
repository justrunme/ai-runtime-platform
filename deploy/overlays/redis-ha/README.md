# Redis for production Runtime

This overlay **does not** ship a Redis operator or a working Sentinel topology.
Own-the-Redis HA is out of scope for the Execution Plane.

## Contract

Production gateways consume Redis only through Secret `ai-runtime-redis` /
`REDIS_URL` (prefer `rediss://` with ACL credentials).

| Concern | Expectation |
| --- | --- |
| Topology | Managed Redis / MemoryDB / ElastiCache / Memorystore / Azure Cache |
| Auth | Password or ACL required |
| Transport | TLS (`rediss://`) in production |
| Durability | Operator-defined RPO; gateway keys are TTL operational state, not audit |
| Client | Standard Redis URL (not Sentinel-aware multi-endpoint discovery) |

## Demo vs production

| Overlay | Purpose |
| --- | --- |
| `deploy/overlays/demo-redis` | Single-replica, no auth/TLS — local/demo only |
| `deploy/overlays/production` | Expects external Redis Secret |

## Example Secret

See `deploy/overlays/production/secrets.example.yaml`.
