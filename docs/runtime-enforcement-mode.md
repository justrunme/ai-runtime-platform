# Runtime enforcement mode

The AI Runtime Gateway can act as a **policy enforcement point** for inference traffic by calling the [AI Infrastructure Control Plane](https://github.com/justrunme/ai-infra-control-plane) before executing a chat completion.

```text
Client request
  -> AI Runtime Gateway
  -> POST /governance/evaluate (control plane)
  -> allow | approval_required | block
  -> upstream model backend (only when allowed)
```

This turns the runtime layer from a routing proxy into an enterprise-style AI platform boundary: the control plane decides, the runtime enforces.

## Enable enforcement

Set the control plane base URL on the gateway:

```bash
export CONTROL_PLANE_URL=http://ai-control-plane:8080
```

When `CONTROL_PLANE_URL` is set, the gateway evaluates every `/v1/chat/completions` request before routing or upstream execution.

| Variable | Default | Purpose |
| --- | --- | --- |
| `CONTROL_PLANE_URL` | unset | Base URL of the control plane API |
| `GOVERNANCE_ENFORCEMENT` | `true` | Set `false` to keep the URL configured but skip checks |
| `GOVERNANCE_FAIL_OPEN` | `false` | Allow inference when the control plane is unavailable |
| `GOVERNANCE_TIMEOUT_SECONDS` | `2.0` | HTTP timeout for governance evaluation |
| `GOVERNANCE_DEFAULT_TEAM` | `platform` | Default team when no header is present |
| `GOVERNANCE_DEFAULT_OWNER` | `gateway` | Default owner |
| `GOVERNANCE_DEFAULT_ENVIRONMENT` | `development` | Default environment |
| `GOVERNANCE_DEFAULT_NAMESPACE` | `ai-dev` | Default Kubernetes namespace |
| `GOVERNANCE_DEFAULT_PROVIDER` | `ollama` | Default model provider label |
| `GOVERNANCE_DEFAULT_COST_PER_HOUR_USD` | `0.18` | Default burn-rate input |
| `GOVERNANCE_DEFAULT_MONTH_TO_DATE_COST_USD` | `100` | Default month-to-date spend |
| `GOVERNANCE_DEFAULT_FORECAST_MONTHLY_COST_USD` | `400` | Default forecast spend |

## Request attribution headers

The gateway maps OpenAI chat payloads and optional headers into the control plane governance contract:

| Header | Maps to |
| --- | --- |
| `x-ai-team` | `team` |
| `x-ai-owner` | `owner` |
| `x-ai-environment` | `environment` |
| `x-ai-namespace` | `namespace` |
| `x-ai-provider` | `provider` |
| `x-ai-action` | `action` |
| `x-ai-sensitive-data` | `sensitive_data` |
| `x-ai-tool-access` | `tool_access` |
| `x-ai-write-permission` | `write_permission` |
| `x-ai-cost-per-hour-usd` | `cost_per_hour_usd` |
| `x-ai-month-to-date-cost-usd` | `month_to_date_cost_usd` |
| `x-ai-forecast-monthly-cost-usd` | `forecast_monthly_cost_usd` |
| `x-ai-model-digest` | `model_artifact_digest` (also forwarded to control plane) |
| `x-ai-model-revision` | `model_revision` (also forwarded to control plane) |
| `Authorization` | forwarded for OIDC JWT identity resolution |

Token counts are estimated from the chat `messages` body and `max_tokens`. Estimated request cost uses configured model unit prices from `MODEL_TARGETS`.

## Model supply chain headers

When the control plane signed model registry is enabled, clients (or sidecars) can attach the artifact they intend to serve:

```bash
curl -sS -X POST http://127.0.0.1:8090/v1/chat/completions \
  -H 'content-type: application/json' \
  -H 'x-ai-team: platform' \
  -H 'x-ai-model-digest: sha256:e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855' \
  -H 'x-ai-model-revision: v1' \
  -d '{"model":"llama3.1:8b","messages":[{"role":"user","content":"hello"}]}'
```

The gateway includes digest/revision in the governance evaluate JSON body and forwards the same headers to the control plane for registry attestation checks.

## Verdict handling

| Control plane verdict | Gateway response |
| --- | --- |
| `allow` | Normal routing and upstream execution |
| `block` | `403` with governance reasons and stage details |
| `approval_required` | `409` with governance reasons and stage details |
| control plane unavailable | `503` unless `GOVERNANCE_FAIL_OPEN=true` |

## Metrics

Prometheus counter:

```text
gateway_governance_decisions_total{verdict="allow|block|approval_required|control_plane_error|fail_open", team="..."}
```

Scrape `/metrics` on the gateway alongside existing routing metrics.

## Local demo

Run the control plane and gateway together:

```bash
# Terminal 1: control plane
cd ../ai-infra-control-plane
make run

# Terminal 2: runtime gateway with enforcement
cd ../ai-runtime-platform
export CONTROL_PLANE_URL=http://127.0.0.1:8080
export MODEL_TARGETS='{"qwen2.5:1.5b":{"url":"http://127.0.0.1:11434/v1","input_cost_per_million":0,"output_cost_per_million":0}}'
uvicorn app.gateway.main:app --reload --port 8090
```

Trigger a block from the governance playground preset or send a high-risk request:

```bash
curl -sS -X POST http://127.0.0.1:8090/v1/chat/completions \
  -H 'content-type: application/json' \
  -H 'x-ai-team: research' \
  -H 'x-ai-sensitive-data: true' \
  -H 'x-ai-write-permission: true' \
  -d '{"model":"qwen2.5:1.5b","messages":[{"role":"user","content":"hello"}]}'
```

## Platform architecture

Pair this runtime adapter with the control plane governance playground, inventory drift detection, and forecasting APIs to present a full private AI platform story:

- **Runtime** (`ai-runtime-platform`): executes inference, routes traffic, enforces verdicts
- **Control plane** (`ai-infra-control-plane`): observes inventory, evaluates policy, exposes governance API

See also: [AI Infrastructure Control Plane runtime integration](https://github.com/justrunme/ai-infra-control-plane/blob/main/docs/runtime-enforcement.md).
