# Upgrade guide

## To v1.2.0

1. No intentional OpenAPI breaking changes from 1.1; refresh clients only if they parsed FastAPI `detail` wrappers (removed in 1.1).
2. Prefer external Redis Secret over demo Redis.
3. For vLLM chart upgrades, pin `image.digest` when promoting to production GPUs.
4. See [compatibility matrix](compatibility-matrix.md).

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
