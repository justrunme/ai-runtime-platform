# Compatibility

Control Plane × Runtime results from the real Control Plane e2e.
Each image is resolved to a digest and checked with keyless
`cosign verify` (identity = the Control Plane **Release** workflow)
before the stack starts.

Last updated: `2026-10-10T12:35:00Z` (local signature check; e2e from the last green cells on `main`)

| Control Plane | Runtime | Image digest | Signature | e2e |
| --- | --- | --- | --- | --- |
| 2.4.0 | 2.3.x | `sha256:608f7758bd29979c81db146b9d76b83b6a7b906446d31c15dddbabd8c0a13274` | pass | pass |
| main | 2.3.x | `sha256:2444671ff06dbdcff8a3ca6029aeb3a5bb7513b4c3bc821bd4f150470a86d194` | fail | skipped |
| 2.0.0 | 2.3.x | `sha256:38b4de3e5bbcbfcb4012d9b16650139eb58171cec29aed7424dad807100f0450` | pass | pass |
| 1.3.0 | 2.3.x | `sha256:29d282ab7d0b60742e56e44aaeca76054cca4d5358d165f80ae47c37ae3c4ec6` | pass | pass |

## Pins

- `2.4.0` → `ghcr.io/justrunme/ai-infra-control-plane@sha256:608f7758bd29979c81db146b9d76b83b6a7b906446d31c15dddbabd8c0a13274`
- `main` → `ghcr.io/justrunme/ai-infra-control-plane:main` (resolved at runtime; current tip unsigned)
- `2.0.0` → `ghcr.io/justrunme/ai-infra-control-plane@sha256:38b4de3e5bbcbfcb4012d9b16650139eb58171cec29aed7424dad807100f0450`
- `1.3.0` → `ghcr.io/justrunme/ai-infra-control-plane@sha256:29d282ab7d0b60742e56e44aaeca76054cca4d5358d165f80ae47c37ae3c4ec6`

`main` is published (multi-arch) but the latest Control Plane Release run
failed at provenance attest **before** `cosign sign`, so the floating tag
has no keyless signature. The e2e cell is skipped until the Release
workflow signs the tip.

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
