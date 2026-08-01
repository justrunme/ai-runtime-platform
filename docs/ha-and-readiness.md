# HA profiles and readiness

## Gateway profiles (`GATEWAY_PROFILE`)

| Profile | Auth | Control Plane | Shared state | Notes |
| --- | --- | --- | --- | --- |
| `local` | Optional | Optional | In-memory allowed | Demo / laptop |
| `internal` | API key **or** JWT required | Optional | Redis if replicas > 1 | Trusted networks |
| `production` | **OIDC/JWKS required** | Required (fail-closed) | Redis required via Secret | No open gateway |

Deploy overlays:

| Overlay | Path | Replicas | Redis |
| --- | --- | --- | --- |
| Single-node | `deploy/overlays/single-node` | 1 | In-memory allowed |
| Production | `deploy/overlays/production` | ≥2 | External Redis Secret (`ai-runtime-redis`) |
| Demo Redis | `deploy/overlays/demo-redis` | 1 Redis | **Reference only** — no PVC, auth, TLS, or HA |

The gateway refuses to start when `GATEWAY_REPLICAS > 1`, `REQUIRE_SHARED_STATE=true`, or `GATEWAY_PROFILE=production` without `REDIS_URL`.

Production Secrets example: `deploy/overlays/production/secrets.example.yaml`.

```bash
kubectl apply -k deploy/overlays/single-node
kubectl apply -f deploy/overlays/production/secrets.example.yaml   # after editing
kubectl apply -k deploy/overlays/production
# optional demo Redis only:
kubectl apply -k deploy/overlays/demo-redis
```

## Redis durability contract

Gateway Redis state (decisions, health signals, tenant counters) is **TTL / operational shared state**, not a permanent audit log. Restarting a non-durable Redis loses recent decisions and quota windows. Production must use managed Redis, Sentinel, or Cluster with persistence/backups appropriate to your RPO — see `deploy/overlays/redis-ha/README.md`.

## Probes

| Endpoint | Purpose |
| --- | --- |
| `/livez` | Process liveness |
| `/readyz` | Config, Redis (if required), Control Plane (fail-closed), usable routes |
| `/healthz` | Alias of `/livez` for compatibility |

`/readyz` may return `status=degraded` when some backends are unhealthy but at least one route remains usable.

## Platform e2e

Combined golden paths: `deploy/e2e/`.
