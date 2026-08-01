# Control Plane ↔ Runtime compatibility

| Control Plane | Runtime | Status |
| --- | --- | --- |
| **2.4.x** | **2.3.x** | **Current stable pair** — release gate + evidence/verify/tenant/MCP/capability digests |
| 2.0.x–2.3.x | 2.3.x | Backward-compatible 2.x contract (evaluate, approval, verify, MCP) |
| 1.3.x | 2.3.x | Legacy compatibility gate (evaluate + approval) |
| 2.x | 1.x | Migration required — see [upgrade guide](upgrade-guide.md) |

Historical pairs (still documented for upgrades):

| Control Plane | Runtime | Notes |
| --- | --- | --- |
| 1.0.x–1.2.x | 1.0.x–1.2.x | Early evaluate path |
| 1.3.x / 1.4.x | 1.4.x | Enforcement evidence headers |
| 1.5.x | 1.5.x | `/v1/runtime/status` + `/v1/runtime/verify` |
| 1.6.x–1.9.x | 1.6.x–1.9.x | Tenant admission, MCP, usage events |
| 1.3.x | 2.0.x | Prior release-gate legacy pin |

Frozen contracts:

- Runtime OpenAPI: `docs/api/openapi.json`
- Approval header: `x-ai-approval-id`
- Evaluate path: `/governance/evaluate`
- Error envelope: top-level `{ "error": { "type", "code", "message" } }`
