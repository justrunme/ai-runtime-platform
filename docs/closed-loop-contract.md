# Closed-loop contract (Runtime 2.0)

Stable Execution Plane contract for Control Plane closed-loop governance.

## Principle

```text
Authenticate → Ask Control Plane → Route → Execute → Observe → Prove
```

Runtime does **not** evaluate PolicyBundles, store billing ledgers, or apply
GitOps remediations over HTTP. It proves what it enforced and loaded.

## Frozen surfaces

| Surface | Path / artifact | Stability |
| --- | --- | --- |
| OpenAPI | `docs/api/openapi.json` | Frozen; body breaks are major |
| Chat | `POST /v1/chat/completions` | OpenAI body + evidence headers |
| Decisions | `GET /v1/decisions/{request_id}` | Tenant-scoped |
| Runtime status | `GET /v1/runtime/status` | Config digest / generation |
| Runtime verify | `POST /v1/runtime/verify` | Expected-vs-actual + optional signed `verification_token` |
| Runtime JWKS | `GET /v1/runtime/jwks` | Public keys for verify token validation |
| MCP tools | `POST /mcp/tools/{tool}/call` | Govern + optional execute (JSON/SSE) |
| MCP servers | `GET /mcp/servers` | Registry metadata + active session |
| MCP session | `POST /mcp/servers/{server}/initialize` | Session + capability digest |
| MCP cancel | `POST /mcp/servers/{server}/cancel` | Cancel in-flight MCP request |
| Health | `/livez`, `/readyz`, `/metrics` | Probe contract |

## Evidence headers (success and governance responses)

```http
x-ai-control-decision-id
x-ai-policy-bundle-id
x-ai-policy-digest
x-ai-request-digest
x-ai-runtime-version
x-ai-approval-id
```

OpenAI response bodies remain free of routing/governance pollution unless
`GATEWAY_EMBED_ROUTING_METADATA` is explicitly enabled.

## Closed loop with Control Plane

```text
CP detects drift / policy gap
  → simulate PolicyBundle impact
  → approval-bound GitOps remediation
  → Argo sync / pod restart
  → Runtime /v1/runtime/verify (correlation + optional verification_token)
  → CP validates token via /v1/runtime/jwks
  → Runtime evidence headers + usage events prove execution
```

### Signed verify (optional)

Set `RUNTIME_VERIFY_PRIVATE_KEY` (PEM), `RUNTIME_VERIFY_PRIVATE_KEY_B64`, or
`RUNTIME_VERIFY_PRIVATE_KEY_FILE`. Responses include `verification_token` (RS256)
with `observed`, `correlation` (generation/digests), and optional
`remediation_id` / `correlation_id`. Enable `GATEWAY_REQUIRE_SIGNED_VERIFY=true`
to fail closed when signing is unavailable.

### HA proof

Nightly workflow `platform-e2e-ha.yaml` runs Postgres-backed dual Control Plane,
dual Runtime, Redis, OIDC: cross-CP approval redemption, signed verify,
tenant isolation across replicas, and SIGTERM during stream.

## Compatibility

See [compatibility matrix](compatibility-matrix.md). Runtime `2.2.x` pairs with
Control Plane `2.0+` for HA/Postgres closed-loop proofs and `1.5+` for
status/verify; MCP execution requires CP evaluate-tool + registry alignment.
