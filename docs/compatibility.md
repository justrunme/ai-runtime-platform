# Compatibility

Control Plane × Runtime results from the real Control Plane e2e.
Each image is resolved to a digest and checked with keyless `cosign verify` (identity = the Control Plane **Release** workflow) before the stack starts.

Last updated: `2026-10-10T12:45:46Z`
Workflow run: https://github.com/justrunme/ai-runtime-platform/actions/runs/38053041482

| Control Plane | Runtime | Image digest | Signature | e2e |
| --- | --- | --- | --- | --- |
| 2.4.0 | 2.3.x | `sha256:608f7758bd29979c81db146b9d76b83b6a7b906446d31c15dddbabd8c0a13274` | pass | pass |
| main | 2.3.x | `sha256:9afcd28d754f69a3ee97b91de0c3066077aa18c1900a9c2cde18bcff7059dee0` | pass | pass |
| 2.0.0 | 2.3.x | `sha256:38b4de3e5bbcbfcb4012d9b16650139eb58171cec29aed7424dad807100f0450` | pass | pass |
| 1.3.0 | 2.3.x | `sha256:29d282ab7d0b60742e56e44aaeca76054cca4d5358d165f80ae47c37ae3c4ec6` | pass | pass |

## Pins

- `2.4.0` → `ghcr.io/justrunme/ai-infra-control-plane@sha256:608f7758bd29979c81db146b9d76b83b6a7b906446d31c15dddbabd8c0a13274`
- `main` → `ghcr.io/justrunme/ai-infra-control-plane@sha256:9afcd28d754f69a3ee97b91de0c3066077aa18c1900a9c2cde18bcff7059dee0`
- `2.0.0` → `ghcr.io/justrunme/ai-infra-control-plane@sha256:38b4de3e5bbcbfcb4012d9b16650139eb58171cec29aed7424dad807100f0450`
- `1.3.0` → `ghcr.io/justrunme/ai-infra-control-plane@sha256:29d282ab7d0b60742e56e44aaeca76054cca4d5358d165f80ae47c37ae3c4ec6`

## Verify locally

```sh
cosign verify \
  --certificate-identity-regexp \
  '^https://github.com/justrunme/ai-infra-control-plane/\.github/workflows/release\.ya?ml@refs/(heads/main|tags/v.+)$' \
  --certificate-oidc-issuer https://token.actions.githubusercontent.com \
  ghcr.io/justrunme/ai-infra-control-plane@<digest>
```

Supported-pair policy (beyond the last CI run) lives in
[compatibility-matrix.md](compatibility-matrix.md).
