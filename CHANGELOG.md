# Changelog

## 1.2.0

Architecture and supply-chain hardening on the frozen OpenAPI surface.

- Router/service split: `routers/*`, `services/*`, `stores/health.py`, `config.py`, `metrics.py`
- Redis HA reference overlay (`deploy/overlays/redis-ha`)
- vLLM chart 0.3: image digest, NetworkPolicy, ServiceAccount, imagePullSecrets, PVC keep policy, GPU profiles
- Compatibility matrix and OCI Helm publication notes

## 1.1.0

Production proof release.

- `GATEWAY_PROFILE` modes: `local` / `internal` / `production` (fail-closed auth + CP + Redis)
- Production overlay uses external Redis/OIDC/Control Plane Secrets; demo Redis moved to `demo-redis`
- OpenAI-compatible top-level error envelope (no FastAPI `detail` wrapper)
- Streaming interruption classification (`upstream_interrupted` vs `client_disconnected`, …)
- Combined platform e2e (`deploy/e2e`) + CI job
- ASGI golden paths for allow/block/approval/fail-closed
- SLO targets wording + `scripts/slo_benchmark.py` / k6 harness

## 1.0.0

Stable Execution Plane boundary for AI Infrastructure OS with Control Plane 1.x.

- Trust boundary: fail-closed JWT, trusted-proxy mode, server-derived governance attributes
- Control Plane v1 approval contract (`x-ai-approval-id` forward + structured 409)
- HA profiles with Redis-required multi-replica readiness
- Observed streaming lifecycle and frozen OpenAI-compatible API
- Production vLLM Helm chart

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
