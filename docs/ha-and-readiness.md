# HA profiles and readiness

## Profiles

| Profile | Path | Replicas | Shared state |
| --- | --- | --- | --- |
| Single-node | `deploy/overlays/single-node` | 1 | In-memory allowed |
| Production | `deploy/overlays/production` | ≥2 | `REDIS_URL` required |

The gateway refuses to start when `GATEWAY_REPLICAS > 1` or `REQUIRE_SHARED_STATE=true` without `REDIS_URL`.

```bash
kubectl apply -k deploy/overlays/single-node
kubectl apply -k deploy/overlays/production
```

## Probes

| Endpoint | Purpose |
| --- | --- |
| `/livez` | Process liveness |
| `/readyz` | Config, Redis (if required), Control Plane (fail-closed), usable routes |
| `/healthz` | Alias of `/livez` for compatibility |

`/readyz` may return `status=degraded` when some backends are unhealthy but at least one route remains usable.
