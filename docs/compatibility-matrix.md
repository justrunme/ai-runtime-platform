# Control Plane ↔ Runtime compatibility

| Control Plane | Runtime | Status |
| --- | --- | --- |
| 1.0.x | 1.0.x | Supported |
| 1.1.x | 1.0.x | Backward compatible |
| 1.x | 1.1.x / 1.2.x / 1.3.x | Supported (evaluate + `x-ai-approval-id`) |
| 1.3.x / 1.4.x | 1.4.x | Supported + enforcement evidence headers; signed `decision_token` optional until CP emits it |
| 1.5.x | 1.5.x | Supported + `/v1/runtime/status` and `/v1/runtime/verify` for remediation close-loop |
| 1.6.x | 1.6.x | Supported + tenant-scoped decisions/admission; CP tenant RBAC should align claim names |
| 2.x | 1.x | Requires migration guide |

Frozen contracts:

- Runtime OpenAPI: `docs/api/openapi.json`
- Approval header: `x-ai-approval-id`
- Evaluate path: `/governance/evaluate`
- Error envelope: top-level `{ "error": { "type", "code", "message" } }`
