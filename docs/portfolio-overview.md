# AI Infrastructure OS — Execution Plane

This repository is the **Execution Plane** of the [AI Infrastructure OS](https://github.com/justrunme/ai-infra-control-plane/blob/main/docs/product-roadmap.md).

The **Control Plane** ([ai-infra-control-plane](https://github.com/justrunme/ai-infra-control-plane)) evaluates policy. This repository executes inference and enforces verdicts when `CONTROL_PLANE_URL` is configured.

## Role in the platform

```text
AI Infrastructure OS
├── Execution Plane       → this repository
├── Control Plane         → ai-infra-control-plane
├── Policy Engine         → governance/ (control plane) + runtime enforcement
├── Tool & Agent Layer    → MCP proxy + intent proxy + governed tool calls
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
- Governed MCP tool calls (`/mcp/tools/{tool}/call`)
- Intent resolution proxy (`/v1/intent/resolve`)
- Tenant attribution with optional Redis shared state (`TENANT_ATTRIBUTION_ENABLED`, `REDIS_URL`, `gateway_tenant_*` metrics)
- OIDC/JWKS verification and Authorization forwarding
- Post-response evaluation submission to the Control Plane
- Canary promotion analysis (`experiments/canary-analysis/`)
- vLLM, KServe, KEDA, OpenTelemetry reference deployments

## Integration

```text
Client → Execution Plane (gateway) → Control Plane /governance/evaluate → model backend
Agent intent → Execution Plane /v1/intent/resolve → Control Plane /intent/resolve
Tool call → Execution Plane /mcp/tools/{tool}/call → Control Plane /governance/evaluate-tool
```

See [runtime enforcement mode](runtime-enforcement-mode.md) and the [control plane portfolio overview](https://github.com/justrunme/ai-infra-control-plane/blob/main/docs/portfolio-overview.md).
