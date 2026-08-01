# Upgrade guide to v1.0.0

## From 0.5.x / 0.6.x

1. Deploy Redis and set `REDIS_URL` before raising `GATEWAY_REPLICAS` above 1.
2. Point probes to `/livez` and `/readyz`.
3. Expect routing metadata in response headers (`x-selected-backend`, `x-routing-reason`, …), not in the OpenAI JSON body unless `GATEWAY_EMBED_ROUTING_METADATA=true`.
4. For JWT mode set `OIDC_JWT_VERIFY=true` and `OIDC_JWKS_URL`; do not rely on spoofable `x-ai-*` identity headers.
5. Local demos that inject identity headers need `IDENTITY_TRUSTED_PROXY=true`.
6. Approval flow: handle `409` with `approval_id`, approve in Control Plane, retry with `x-ai-approval-id`.

## Breaking changes since early 0.1

- Invalid JWT no longer falls open to header identity
- Client cost/sensitive headers no longer drive policy fields
- Streaming success is recorded after body completion
- Default base deploy is single-replica; HA requires production overlay + Redis
