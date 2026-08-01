# Runtime enforcement mode

The AI Runtime Gateway acts as a **policy enforcement point** for inference traffic by calling the [AI Infrastructure Control Plane](https://github.com/justrunme/ai-infra-control-plane) before executing a chat completion.

```text
Client request
  -> AI Runtime Gateway
  -> authenticate identity (JWT / trusted-proxy / defaults)
  -> POST /governance/evaluate (control plane)
  -> allow | approval_required | block
  -> upstream model backend (only when allowed)
```

## Approval lifecycle (Control Plane v1)

```text
1) Client -> Gateway chat completion
2) Gateway -> Control Plane /governance/evaluate
3) Control Plane -> approval_required + approval_id
4) Gateway -> client 409 with approval_id, decision_id, retry instructions
5) Client/operator -> Control Plane POST /approvals/{id}/approve
6) Client -> Gateway retry with header x-ai-approval-id
7) Gateway forwards x-ai-approval-id to evaluate
8) Control Plane -> allow (approval consumed) -> inference
```

Example 409 body fields:

- `approval_id`, `decision_id`, `policy_digest`, `request_digest`
- `retry.header` = `x-ai-approval-id`
- response headers `x-ai-approval-id`, `x-ai-decision-id`

## Identity trust boundary

| Mode | Env | Behavior |
| --- | --- | --- |
| JWT verify | `OIDC_JWT_VERIFY=true` + `OIDC_JWKS_URL` | Bearer required; invalid/missing token → `401`; client identity headers ignored |
| Trusted proxy | `IDENTITY_TRUSTED_PROXY=true` (JWT off) | Accept `x-ai-*` identity headers from mesh/proxy |
| Defaults | neither | Use server defaults only; spoofed headers ignored |

Production recommendation: enable JWKS verification and keep `IDENTITY_TRUSTED_PROXY=false` unless identity is injected by a verified mesh sidecar.

## Server-derived governance attributes

Policy evaluation fields are **not** taken from untrusted client cost/risk headers.

| Field | Source |
| --- | --- |
| subject/groups/team | Verified JWT, or trusted-proxy headers, or defaults |
| model/provider | Request model + server `MODEL_TARGETS` registry |
| token/cost estimates | Prompt estimate + model unit prices |
| usage/quota | Runtime tenant store (`requests_last_minute`, `tokens_today`) |
| model digest/revision | Server model target, optionally trusted-proxy headers |
| sensitive/tool/write flags | JWT claims only (default false) |

Client hints (`x-ai-cost-*`, `x-ai-sensitive-data`, …) are forwarded only inside `untrusted_context` for audit visibility.

## MCP governance reference

`POST /mcp/tools/{tool}/call` evaluates Control Plane `/governance/evaluate-tool`, then executes against a registered MCP server when `mcp_server` is set and `MCP_SERVER_REGISTRY` defines that server (Streamable HTTP + `env:` credential refs). Without a registry entry the call remains governance-only (`governed_allowed`).

```bash
curl -sS -X POST http://127.0.0.1:8090/mcp/tools/jira-read/call \
  -H 'content-type: application/json' \
  -H 'x-ai-team: platform' \
  -d '{"action":"read","arguments":{"issue":"PROJ-1"}}'
```

## Enable enforcement

```bash
export CONTROL_PLANE_URL=http://ai-control-plane:8080
```

| Variable | Default | Purpose |
| --- | --- | --- |
| `CONTROL_PLANE_URL` | unset | Base URL of the control plane API |
| `GOVERNANCE_ENFORCEMENT` | `true` | Set `false` to keep the URL configured but skip checks |
| `GOVERNANCE_FAIL_OPEN` | `false` | Allow inference when the control plane is unavailable |
| `GOVERNANCE_TIMEOUT_SECONDS` | `2.0` | HTTP timeout for governance evaluation |
| `OIDC_JWT_VERIFY` | `false` | Fail-closed JWT authentication |
| `OIDC_JWKS_URL` | unset | JWKS endpoint required when verify is enabled |
| `IDENTITY_TRUSTED_PROXY` | `false` | Allow identity headers when JWT verify is off |
| `GOVERNANCE_DEFAULT_TEAM` | `platform` | Default team |
| `GOVERNANCE_DEFAULT_OWNER` | `gateway` | Default owner |
| `GOVERNANCE_DEFAULT_ENVIRONMENT` | `development` | Default environment |
| `GOVERNANCE_DEFAULT_NAMESPACE` | `ai-dev` | Default Kubernetes namespace |
| `GOVERNANCE_DEFAULT_PROVIDER` | `ollama` | Default model provider label |
| `GOVERNANCE_DEFAULT_COST_PER_HOUR_USD` | `0.18` | Server burn-rate input |
| `GOVERNANCE_DEFAULT_MONTH_TO_DATE_COST_USD` | `100` | Server month-to-date spend |
| `GOVERNANCE_DEFAULT_FORECAST_MONTHLY_COST_USD` | `400` | Server forecast spend |

## Verdict handling

| Control plane verdict | Gateway response |
| --- | --- |
| `allow` | Normal routing and upstream execution |
| `block` | `403` with governance error envelope |
| `approval_required` | `409` with approval/decision ids and retry instructions |
| control plane unavailable | `503` unless `GOVERNANCE_FAIL_OPEN=true` |

## Metrics

```text
gateway_governance_decisions_total{verdict="allow|block|approval_required|control_plane_error|fail_open", team="..."}
```

## Local demo

```bash
# Terminal 1: control plane
cd ../ai-infra-control-plane
make run

# Terminal 2: gateway
export CONTROL_PLANE_URL=http://127.0.0.1:8080
export IDENTITY_TRUSTED_PROXY=true
uvicorn app.gateway.main:app --port 8090
```
