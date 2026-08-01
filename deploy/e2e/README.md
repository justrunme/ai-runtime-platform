# Platform e2e (Runtime + Control Plane stub)

Proves combined golden paths for Execution Plane v1.1+:

- allow → inference
- block → model backend not reached (403)
- approval_required → approve → retry with `x-ai-approval-id`
- decision written on gateway-a readable from gateway-b (shared Redis)
- Redis down → `/readyz` 503
- Control Plane down → chat 503 (fail-closed)

## Run

```bash
docker compose -f deploy/e2e/docker-compose.yaml up --build -d
./deploy/e2e/run_e2e.sh
docker compose -f deploy/e2e/docker-compose.yaml down -v
```

Pinned stack intent:

| Component | Version |
| --- | --- |
| Runtime gateway | image built from this repo (1.1+) |
| Control Plane | mock stub compatible with CP evaluate/approve contract |
| Redis | 7.4 |
| Mock OpenAI | FastAPI stub |

For Kind with real Control Plane images, pin `control-plane:1.x` and `runtime-platform:1.x` in your cluster manifests and reuse the same script assertions against the Service URLs.
