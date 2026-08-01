# Runtime status and verification

Control Plane GitOps remediations need proof that the Execution Plane loaded the
intended configuration. Runtime exposes a stable status contract for that.

## Status

```http
GET /v1/runtime/status
```

Returns:

- `runtime_version` / `instance_id`
- `configuration.observed_digest` + `generation` + `loaded_at`
- `last_known_good` / optional `rejected`
- `policy.last_seen_*` from recent governance decisions
- `routes.digest` + model list
- backend healthy/unhealthy/unknown counts

Configuration digest covers non-secret settings (models, routes, profile, timeouts,
auth/control-plane flags). API keys are never hashed into the digest.

## Verify

```http
POST /v1/runtime/verify
Content-Type: application/json

{
  "expected": {
    "config_digest": "sha256:...",
    "generation": 42,
    "models": ["qwen"],
    "policy_digest": "sha256:...",
    "routes_digest": "sha256:..."
  }
}
```

Response:

```json
{
  "verified": false,
  "differences": [
    {
      "field": "models",
      "expected": ["qwen"],
      "actual": ["qwen", "unknown-model"]
    }
  ]
}
```

Omitted expected fields are not checked.

## Atomic snapshots

Today configuration is loaded at process start (ConfigMap/env → restart). Runtime
still treats the loaded view as an immutable snapshot with:

- content digest
- generation (`GATEWAY_CONFIG_GENERATION`)
- last-known-good (= active at boot until dynamic apply exists)
- rejected placeholder for future reject-on-validate paths

## Lifecycle

```text
proposal → PR → Argo sync → Runtime verify → verified
```
