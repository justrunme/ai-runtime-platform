# Maturity matrix

| Component | Status | Notes |
| --- | --- | --- |
| OpenAI chat/completions gateway | Supported | Frozen OpenAPI, SDK tests |
| Control Plane v1 evaluate/approval | Supported | `x-ai-approval-id` redemption |
| JWT/JWKS identity | Supported | Fail-closed when enabled |
| Redis shared state (HA) | Supported | Required for replicas > 1; in-cluster demo Redis is reference-only |
| Production profile fail-closed | Supported | OIDC/JWKS + iss/aud + Control Plane + Redis Secrets |
| API auth middleware | Supported | All non-public routes; claims on `request.state` |
| Platform combined e2e | Supported | mock CP (PR) + OIDC + real CP (nightly/tag) |
| OpenAI error envelope | Supported | Top-level `{error}` contract tests |
| `/livez` `/readyz` | Supported | Production probes |
| Observed streaming lifecycle | Supported | TTFT + terminal outcomes |
| vLLM Helm chart | Supported | Production values profile |
| Tenant attribution | Supported | Redis or in-memory |
| Intent resolve proxy | Reference | Depends on Control Plane |
| MCP tool endpoint | Reference | Governance stub, not full MCP transport |
| KServe examples | Reference | Not primary path |
| KEDA ScaledObject | Reference | Queue autoscaling sample |
| Argo Rollouts canary | Experimental | Example only |

Supported means covered by tests/docs and intended for production profiles. Reference means usable examples. Experimental means may change without notice.
