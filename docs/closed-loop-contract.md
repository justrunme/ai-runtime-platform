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
| Runtime verify | `POST /v1/runtime/verify` | Expected-vs-actual |
| MCP tools | `POST /mcp/tools/{tool}/call` | Govern + optional execute |
| MCP servers | `GET /mcp/servers` | Registry metadata |
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
  → Runtime /v1/runtime/verify
  → Runtime evidence headers + usage events prove execution
```

## Compatibility

See [compatibility matrix](compatibility-matrix.md). Runtime `2.0.x` pairs with
Control Plane `1.5+` for status/verify and `1.4+` for evidence digests; MCP
execution requires CP evaluate-tool + registry alignment.
