# MCP execution transport

Control Plane authorizes tool capability. Runtime executes the transport.

## Sequence

```text
Agent → Runtime tool call
  → Control Plane evaluate-tool (signed verdict path when enabled)
  → Runtime initialize (when sessionMode requires it) + Mcp-Session-Id
  → Runtime MCP Streamable HTTP tools/call (JSON or SSE)
  → audit-tool (session / capability / request ids)
  → filtered/truncated result to agent
```

## Registry

```bash
MCP_SERVER_REGISTRY='{
  "docs": {
    "url": "http://mcp-docs.svc:8080/mcp",
    "transport": "streamable-http",
    "sessionMode": "auto",
    "credentialRef": "env:MCP_DOCS_TOKEN",
    "timeoutSeconds": 30,
    "maxResultBytes": 1048576,
    "capabilityDigest": "sha256:..."
  }
}'
MCP_DOCS_TOKEN=...
```

| Field | Notes |
| --- | --- |
| `transport` | `http`, `streamable-http`, or `sse` |
| `sessionMode` | `auto` (session for streamable-http/sse), `always`, `never` |
| `credentialRef` | `env:NAME` only (no inline secrets) |
| `capabilityDigest` | Optional pin after initialize |

## Call

```http
POST /mcp/tools/search/call
{
  "mcp_server": "docs",
  "action": "invoke",
  "arguments": {"q": "runtime status"}
}
```

Without `mcp_server` / registry: governance-only `governed_allowed` response.

Execution without Control Plane is rejected (`503 mcp_governance_required`) unless
`MCP_ALLOW_UNGOVERNED=true` for local demos.

## Session / cancel

```http
POST /mcp/servers/docs/initialize
POST /mcp/servers/docs/cancel
{"request_id":"<mcp_request_id>"}
GET /mcp/servers
```

`GET /mcp/servers` requires platform-admin or auditor roles when JWT RBAC is enforced.
Responses include active `session_id` when a session is cached in-process.

Transport maturity: **JSON + session/SSE Streamable HTTP** are supported paths.
