# Platform e2e

## PR / every commit (mock Control Plane)

```bash
docker compose -f deploy/e2e/docker-compose.yaml up --build -d
./deploy/e2e/run_e2e.sh
docker compose -f deploy/e2e/docker-compose.yaml down -v
```

Covers allow/block (with backend `/stats`), approval binding/replay, streaming `[DONE]`,
cross-replica decisions, gateway stop, Redis `/readyz` 503, CP fail-closed 503.

## OIDC production profile

```bash
docker compose \
  -f deploy/e2e/docker-compose.yaml \
  -f deploy/e2e/docker-compose.oidc.yaml \
  up --build -d
./deploy/e2e/run_e2e_oidc.sh
```

Proves JWT middleware on `/v1/models` and governed chat: missing/invalid/expired/wrong
issuer/audience → `401`; valid token → `200`.

## Release / nightly (real Control Plane)

```bash
docker compose \
  -f deploy/e2e/docker-compose.yaml \
  -f deploy/e2e/docker-compose.oidc.yaml \
  -f deploy/e2e/docker-compose.real-cp.yaml \
  up --build -d
./deploy/e2e/run_e2e_real_cp.sh
```

Uses `ghcr.io/justrunme/ai-infra-control-plane:1.3.0` by default (`CONTROL_PLANE_IMAGE` override).
Nightly/manual: `.github/workflows/platform-e2e-real-cp.yaml`.
Tag release: reusable workflow is a hard gate before image/chart publish.

| Component | Version intent |
| --- | --- |
| Runtime gateway | image built from this repo |
| Control Plane | published `1.x` image (real-cp) or mock stub (PR) |
| Redis | 7.4 |
| Mock OpenAI / OIDC | FastAPI stubs |
