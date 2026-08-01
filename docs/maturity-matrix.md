# Maturity matrix

| Component | Status | Notes |
| --- | --- | --- |
| OpenAI chat/completions gateway | Supported | Frozen OpenAPI, SDK tests |
| Control Plane evaluate/approval | Supported | `x-ai-approval-id` redemption; release e2e vs CP 2.0 + 1.3 |
| JWT/JWKS identity | Supported | Fail-closed when enabled; iss/aud in production |
| Enforcement evidence headers | Supported | Decision/policy/request digests; OpenAI body clean |
| Runtime status / verify | Supported | GitOps close-loop; RBAC when JWT verify on |
| Tenant-scoped decisions + admission | Supported | JWT tenant; per-replica concurrency limits |
| Global admission / circuit / drain | Supported | Process-local capacity + SIGTERM drain |
| Signed decision token | Supported | Optional; local request-digest binding when present |
| Redis shared state (HA) | Supported | Required for replicas > 1; demo Redis is reference-only |
| Production profile fail-closed | Supported | OIDC/JWKS + iss/aud + Control Plane + Redis Secrets |
| API auth middleware | Supported | All non-public routes; claims on `request.state` |
| Platform combined e2e | Supported | mock CP (PR) + OIDC + real CP (release/nightly) |
| OpenAI error envelope | Supported | Top-level `{error}` contract tests |
| `/livez` `/readyz` | Supported | Production probes; draining status |
| Observed streaming lifecycle | Supported | TTFT + terminal outcomes |
| Usage events | Reference | OTEL + webhook buffer; streaming usage / retry worker pending |
| MCP Streamable HTTP (JSON) | Reference | Stateless `tools/call`; requires CP unless `MCP_ALLOW_UNGOVERNED` |
| MCP session / SSE lifecycle | Experimental | Not implemented |
| Tenant upstream credentials | Experimental | `upstreamCredentialRef` reserved |
| Intent resolve proxy | Reference | Depends on Control Plane |
| vLLM Helm chart | Supported | OCI publish + cosign |
| KServe examples | Reference | Not primary path |
| KEDA ScaledObject | Reference | Queue autoscaling sample |
| Argo Rollouts canary | Experimental | Example only |

Supported means covered by tests/docs and intended for production profiles. Reference means usable examples. Experimental means may change without notice.
