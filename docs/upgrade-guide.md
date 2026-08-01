# Upgrade guide

## To v1.3.1

1. No API or config breaking changes from 1.3.0.
2. Tag releases now publish image/chart only after real Control Plane compatibility proof succeeds.
3. Operators should prefer `1.3.1` over `1.3.0` for supply-chain integrity (same runtime auth/governance surface).

## To v1.3.0

1. All non-public routes require auth when JWT or API keys are configured — including `/v1/models` and `/v1/decisions/{id}`.
2. Production must set `OIDC_JWT_ISSUER` and `OIDC_JWT_AUDIENCE` (see `secrets.example.yaml`).
3. Prefer managed Redis; demo Redis remains demo-only.
4. Release/nightly workflow validates against published Control Plane images.
5. See [auth boundary](auth-boundary.md) and [compatibility matrix](compatibility-matrix.md).

## To v1.2.0

1. No intentional OpenAPI breaking changes from 1.1; refresh clients only if they parsed FastAPI `detail` wrappers (removed in 1.1).
2. Prefer external Redis Secret over demo Redis.
3. For vLLM chart upgrades, pin `image.digest` when promoting to production GPUs.

## To v1.1.0

1. Set `GATEWAY_PROFILE=production` only with OIDC/JWKS, `CONTROL_PLANE_URL`, and `REDIS_URL` Secrets.
2. Move off in-overlay Redis: use `deploy/overlays/demo-redis` for demos, external Redis for production.
3. Expect OpenAI-style errors at the top level (`error`), not under `detail`.
4. Run `deploy/e2e` after deploy.

## To v1.0.0

1. Deploy Redis and set `REDIS_URL` before raising `GATEWAY_REPLICAS` above 1.
2. Point probes at `/livez` and `/readyz`.
3. Forward `x-ai-approval-id` on retries after `409 approval_required`.
4. Treat MCP endpoints as reference maturity.
