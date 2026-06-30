# AI Infrastructure OS — Execution Plane

This repository is the **Execution Plane** of the [AI Infrastructure OS](https://github.com/justrunme/ai-infra-control-plane/blob/main/docs/product-roadmap.md).

The **Control Plane** ([ai-infra-control-plane](https://github.com/justrunme/ai-infra-control-plane)) evaluates policy. This repository executes inference and enforces verdicts when `CONTROL_PLANE_URL` is configured.

## Role in the platform

```text
AI Infrastructure OS
├── Execution Plane       → this repository
├── Control Plane         → ai-infra-control-plane
├── Policy Engine         → governance/ (control plane) + runtime enforcement
├── Cost & Chargeback     → gateway cost metrics + tenant attribution
├── Fleet & Topology      → control plane /topology + /drift
├── Capacity Planner      → experiments/ (both repos)
├── Observability & SLO   → observability/slo/ (control plane)
└── GitOps & Security     → deploy/ + gitops/ (runtime), infra/ (control plane)
```

## What runs here

- OpenAI-compatible gateway with routing intelligence
- Canary, shadow, fallback, health-aware, and cost-aware backend selection
- Governance enforcement adapter (`CONTROL_PLANE_URL`)
- Tenant attribution prototype (`TENANT_ATTRIBUTION_ENABLED`, `gateway_tenant_*` metrics)
- Canary promotion analysis (`experiments/canary-analysis/`)
- vLLM, KServe, KEDA, OpenTelemetry reference deployments

## Integration

```text
Client → Execution Plane (gateway) → Control Plane /governance/evaluate → model backend
```

See [runtime enforcement mode](runtime-enforcement-mode.md) and the [control plane portfolio overview](https://github.com/justrunme/ai-infra-control-plane/blob/main/docs/portfolio-overview.md).
