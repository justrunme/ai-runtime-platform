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

Uses `ghcr.io/justrunme/ai-infra-control-plane:2.4.0` by default (`CONTROL_PLANE_IMAGE` override).
Nightly/manual: matrix over CP `2.4.0`, `2.0.0`, `1.3.0`, and `main`.
Tag release: reusable workflow matrix (`2.4.0` + `2.0.0` + `1.3.0`) is a hard gate before image/chart publish.

## Nightly HA closed-loop

```bash
openssl genrsa -out /tmp/runtime-verify-e2e.pem 2048
export RUNTIME_VERIFY_PRIVATE_KEY_B64="$(base64 < /tmp/runtime-verify-e2e.pem | tr -d '\n')"
docker compose \
  -f deploy/e2e/docker-compose.yaml \
  -f deploy/e2e/docker-compose.oidc.yaml \
  -f deploy/e2e/docker-compose.real-cp.yaml \
  -f deploy/e2e/docker-compose.ha.yaml \
  up --build -d
./deploy/e2e/run_e2e_ha.sh
```

Proves Postgres-backed dual Control Plane, dual Runtime, signed verify, tenant
isolation across replicas, and SIGTERM during an in-flight stream.

| Component | Version intent |
| --- | --- |
| Runtime gateway | image built from this repo |
| Control Plane | published `2.x`/`1.x` image (real-cp) or mock stub (PR) |
| Postgres | 16 (HA overlay) |
| Redis | 7.4 |
| Mock OpenAI / OIDC | FastAPI stubs |
