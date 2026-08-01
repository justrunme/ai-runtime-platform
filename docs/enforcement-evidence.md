# Enforcement evidence

Runtime does not evaluate policy bundles. It records which Control Plane decision it
executed and surfaces digests for audit correlation.

## Decision record fields

| Field | Meaning |
| --- | --- |
| `control_plane_decision_id` | Durable CP decision id |
| `policy_bundle_id` | Bundle identifier from evaluate |
| `policy_digest` | Content digest of the effective policy |
| `request_digest` | Canonical digest of the governed request |
| `control_plane_version` | CP version when provided |
| `runtime_version` | Gateway package / override version |
| `enforcement_outcome` | `executed`, `blocked`, `approval_required`, `upstream_error`, `stream_*` |

## Response headers

Attached on chat completions (and governance deny/approval responses). The OpenAI
JSON body is never modified for these fields.

```http
x-ai-control-decision-id: dec_123
x-ai-decision-id: dec_123
x-ai-policy-bundle-id: production-2026-08
x-ai-policy-digest: sha256:...
x-ai-request-digest: sha256:...
x-ai-runtime-version: 1.4.0
```

`x-ai-decision-id` remains as a compatibility alias for `x-ai-control-decision-id`.

## Signed decision token (optional)

When Control Plane returns `decision_token` (JWS), Runtime verifies:

- signature via `CONTROL_PLANE_DECISION_JWKS_URL` (fallback: `OIDC_JWKS_URL`)
- audience (`CONTROL_PLANE_DECISION_AUDIENCE`, default `ai-runtime`)
- expiration
- verdict / decision_id / digest consistency with the evaluate JSON

Set `GATEWAY_REQUIRE_SIGNED_DECISION=true` to reject evaluate responses that omit the token.

When a token is present, Runtime also computes the local canonical request digest
(same binding fields as Control Plane approval binding, excluding live telemetry)
and rejects mismatches with `signed_decision_request_mismatch`. This blocks replay
of a coherent old token+evaluate pair against a different client request.

## Correlation chain

```text
client request
  → Control Plane governance decision (+ optional JWS)
  → Runtime routing decision record
  → inference result
```
