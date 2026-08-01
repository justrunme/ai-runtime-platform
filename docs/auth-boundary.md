# Authentication boundary

All non-public routes pass through `AuthenticationMiddleware` before handlers run.

## Public routes

- `/livez`
- `/readyz`
- `/healthz`
- `/metrics` (scrape; restrict at NetworkPolicy / mesh in production)
- `/v1/runtime/jwks` (public keys for signed verify tokens)

## Protected routes

Everything else when API keys or JWT verify are configured, including:

- `/v1/models`, `/v1/routes`, `/v1/backends/health`
- `/v1/decisions/{id}`, `/v1/chat/completions`, `/v1/intent/resolve`
- `/v1/runtime/status`, `/v1/runtime/verify`
- `/mcp/*` (tools, servers, initialize, cancel)

## Modes

| Mode | Trigger | Behavior |
| --- | --- | --- |
| JWT / OIDC | `OIDC_JWT_VERIFY=true` | Bearer JWT required; iss/aud enforced when required; claims stored on `request.state.identity_claims` |
| API key | `GATEWAY_API_KEYS` and JWT off | Bearer or `X-API-Key` |
| Open (local) | no auth configured | Allowed only for `GATEWAY_PROFILE=local` |

Production profile additionally requires:

- `OIDC_JWKS_URL`
- `OIDC_JWT_ISSUER`
- `OIDC_JWT_AUDIENCE`
- `OIDC_JWT_REQUIRE_ISS_AUD=true` (also implied by `GATEWAY_PROFILE=production`)
- `CONTROL_PLANE_URL`
- `REDIS_URL`

`resolve_workload_identity()` reuses middleware-verified claims and does not perform a second JWKS lookup.
