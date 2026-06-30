# Security Policy

## Reporting a vulnerability

Please report suspected vulnerabilities privately through GitHub Security
Advisories ("Report a vulnerability" on the repository **Security** tab) rather
than opening a public issue. Include reproduction steps and the affected
component (gateway, chart, or manifests). We aim to acknowledge a report within
five business days.

## Supported versions

This is a portfolio/reference project. Only the latest `main` and the most
recent tagged release receive fixes.

| Version | Supported |
| --- | --- |
| `main` / latest tag | yes |
| older tags | no |

## Supply chain

Released container images are built in CI and:

- pushed to `ghcr.io/justrunme/ai-runtime-platform` with build provenance attestations,
- signed with [cosign](https://github.com/sigstore/cosign) using keyless OIDC,
- scanned with [Trivy](https://github.com/aquasecurity/trivy) (build fails on fixable `HIGH`/`CRITICAL`),
- accompanied by an SPDX SBOM published as a build artifact.

Verify a signature with:

```sh
cosign verify ghcr.io/justrunme/ai-runtime-platform:<tag> \
  --certificate-identity-regexp 'https://github.com/justrunme/ai-runtime-platform/.*' \
  --certificate-oidc-issuer https://token.actions.githubusercontent.com
```

## Secrets

No production secret, model licence acceptance, or cloud credential belongs in
this repository. Supply API keys (`GATEWAY_API_KEYS`), registry credentials, and
model-download tokens through the cluster's secret-management and
workload-identity mechanisms.
