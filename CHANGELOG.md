# Changelog

## 1.3.1

Release integrity fix.

- Real Control Plane e2e no longer fails when evaluate response omits `request_digest` (verify via durable decision API)
- Approver JWT sent on approve/reject against real Control Plane
- Clearer HTTP failure dumps in real CP golden-path script
- Release workflow gates image/chart publish on reusable real CP e2e (`needs`)
- Tag no longer runs a parallel ungated real CP workflow

## 1.3.0

Real platform compatibility proof.

- Combined e2e against published Control Plane image (nightly/tag workflow)
- OIDC/JWKS production-profile e2e (missing/invalid/expired/iss/aud)
- Approval binding/replay/body-mismatch coverage (mock + real CP)
- Gateway restart + Control Plane restart paths
- OCI Helm chart publish in release workflow
- SLO evidence scaffolding under `docs/evidence/v1.3.0/`

## 1.2.1

Authentication boundary fix (shipped in the 1.3.0 train).

- Unified `AuthenticationMiddleware` protects all non-public routes
- JWT claims cached on `request.state.identity_claims` (no double JWKS decode)
- Production requires `OIDC_JWT_ISSUER` + `OIDC_JWT_AUDIENCE`
- JWKS lookup via `asyncio.to_thread` + metrics
- Broken Sentinel manifests removed; managed Redis docs only
- vLLM NetworkPolicy egress documented as permissive by default + hardened profile

## 1.2.0

Architecture and supply-chain hardening on the frozen OpenAPI surface.

- Router/service split: `routers/*`, `services/*`, `stores/health.py`, `config.py`, `metrics.py`
- Redis HA reference docs / demo Redis separation
- vLLM chart: image digest, NetworkPolicy, ServiceAccount, imagePullSecrets, PVC keep policy, GPU profiles
- Compatibility matrix and OCI Helm publication notes

## 1.1.0

Production proof release (included in 1.2.0 train).

- `GATEWAY_PROFILE` modes: `local` / `internal` / `production`
- External Redis via Secret; OpenAI error envelope; streaming interruption classification
- Combined platform e2e + CI job; SLO targets wording

## 1.0.0

Stable Execution Plane boundary for AI Infrastructure OS with Control Plane 1.x.

## 0.6.0

Production vLLM Helm hardening.

## 0.5.0

Frozen OpenAPI, clean OpenAI bodies, SDK compatibility tests.

## 0.4.0

Streaming lifecycle correctness.

## 0.3.0

HA Redis profiles and readiness probes.

## 0.2.0

Trust boundary and Control Plane approval contract.

## 0.1.x

Initial Execution Plane gateway and demos.
