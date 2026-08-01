# MCP execution transport

Control Plane authorizes tool capability. Runtime executes the transport.

## Sequence

```text
Agent → Runtime tool call
  → Control Plane evaluate-tool (signed verdict path when enabled)
  → Runtime MCP Streamable HTTP tools/call with scoped credential
  → optional audit-tool
  → filtered/truncated result to agent
```

## Registry

```bash
MCP_SERVER_REGISTRY='{
  "docs": {
    "url": "http://mcp-docs.svc:8080/mcp",
    "transport": "http",
    "credentialRef": "env:MCP_DOCS_TOKEN",
    "timeoutSeconds": 30,
    "maxResultBytes": 1048576
  }
}'
MCP_DOCS_TOKEN=...
```

Credential refs never embed secret values — only `env:NAME` (vault URIs require an injector that materializes env vars).

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

`GET /mcp/servers` requires platform-admin or auditor roles when JWT RBAC is enforced.

Transport maturity: **stateless JSON `tools/call`** is the supported reference path.
Full MCP session/SSE lifecycle remains experimental.
