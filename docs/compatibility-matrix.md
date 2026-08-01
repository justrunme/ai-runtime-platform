# Control Plane ↔ Runtime compatibility

| Control Plane | Runtime | Status |
| --- | --- | --- |
| 1.0.x | 1.0.x | Supported |
| 1.1.x | 1.0.x | Backward compatible |
| 1.x | 1.1.x / 1.2.x | Supported (evaluate + `x-ai-approval-id`) |
| 2.x | 1.x | Requires migration guide |

Frozen contracts:

- Runtime OpenAPI: `docs/api/openapi.json`
- Approval header: `x-ai-approval-id`
- Evaluate path: `/governance/evaluate`
- Error envelope: top-level `{ "error": { "type", "code", "message" } }`
