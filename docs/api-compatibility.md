# Runtime API compatibility

## Contract

- OpenAI-compatible `POST /v1/chat/completions` and `GET /v1/models`
- Frozen OpenAPI snapshot: `docs/api/openapi.json`
- Runtime routing metadata is returned in `X-*` / `X-AI-*` response headers
- Standard OpenAI JSON body is left unchanged unless `GATEWAY_EMBED_ROUTING_METADATA=true`

## Error envelope

Gateway errors use:

```json
{
  "detail": {
    "error": {
      "type": "api_error|authentication_error|governance_error",
      "code": "stable_code",
      "message": "human readable"
    }
  }
}
```

Governance approval responses also include `approval_id`, `decision_id`, and `retry` instructions.

## Upgrade policy

- Additive header/fields are non-breaking
- Removing or renaming response body fields is a major version change
- OpenAPI freeze CI fails when paths/schemas change without updating the snapshot
