# AI Runtime Platform

> Kubernetes-native runtime for private LLM inference with vLLM, KServe, KEDA, OpenTelemetry and GitOps.

This repository demonstrates the runtime layer of an AI platform: receiving an OpenAI-compatible request, routing it to a GPU model server, scaling from inference pressure, and emitting operations-grade telemetry. It deliberately does not present a model governance control plane.

```mermaid
flowchart LR
  Client["Client / OpenAI SDK"] --> Gateway["AI Runtime Gateway"]
  Gateway --> VLLM["vLLM runtime"]
  Gateway --> KServe["KServe InferenceService"]
  VLLM --> GPU["GPU nodes"]
  KServe --> GPU
  VLLM --> Prometheus
  Gateway --> OTel["OpenTelemetry Collector"]
  Prometheus --> KEDA["KEDA autoscaler"]
  KEDA --> VLLM
  ArgoCD["Argo CD"] --> Gateway
  ArgoCD --> VLLM
```

## What is implemented

- OpenAI-compatible `POST /v1/chat/completions` gateway with explicit model-to-backend routing.
- Gateway-generated request ID propagation, OpenTelemetry spans, and estimated cost from the returned token usage.
- Production-oriented vLLM Helm chart: GPU requests/limits, GPU node selection, probes, a Prometheus metrics service, and optional `ServiceMonitor`.
- KServe `InferenceService` example in Standard mode for a generative workload.
- KEDA `ScaledObject` based on vLLM queue pressure (`vllm:num_requests_waiting`), rather than CPU utilization.
- OpenTelemetry Collector configuration, Argo Rollouts canary example, Argo CD application, and a k6 benchmark.
- GitHub Actions validation for Python, Helm, and Kustomize rendering.

## Scope and deployment modes

The Helm chart is the primary vLLM deployment path. The KServe and Argo Rollouts manifests are focused examples of alternative operating modes; they are not intended to be applied together to the same runtime. Likewise, the KEDA target must not be controlled by a second HPA.

| Component | Purpose | Requirement |
| --- | --- | --- |
| Gateway | OpenAI API, routing, trace and cost attribution | Kubernetes + an accessible model backend |
| vLLM chart | Primary GPU inference runtime | NVIDIA device plugin, model access credentials |
| KServe example | Kubernetes-native model lifecycle alternative | KServe >= 0.18 Standard mode |
| KEDA example | Queue-aware scaling | KEDA + Prometheus |
| Observability | traces and model metrics | OpenTelemetry Operator + Prometheus stack |
| GitOps | reconciliation | Argo CD |

## Quick start

Prerequisites: Kubernetes cluster with GPU nodes, NVIDIA device plugin, Helm, `kubectl`, an OCI registry for the gateway image, and model download credentials where the model requires them.

```sh
git clone https://github.com/justrunme/ai-runtime-platform.git
cd ai-runtime-platform

# Local source checks
python -m venv .venv
. .venv/bin/activate
pip install -e '.[dev]'
make validate

# Primary serving path. Review and pin values for the target cluster first.
kubectl apply -f deploy/base/namespace.yaml
helm upgrade --install qwen charts/vllm-runtime \
  --namespace ai-runtime \
  --set model.name=Qwen/Qwen2.5-7B-Instruct \
  --set model.servedName=qwen2.5-7b-instruct

# Build and publish the gateway image before applying deploy/base/gateway.yaml.
```

The chart defaults are intentionally only a starting profile. Production clusters must set the model revision, registry digest, GPU type/count, persistent model-cache strategy, network policy, authentication, and resource sizing through GitOps values.

## Gateway contract

The gateway accepts the standard OpenAI chat-completions shape and reads model targets from `MODEL_TARGETS`:

```json
{
  "qwen2.5-7b-instruct": {
    "url": "http://qwen-vllm-runtime.ai-runtime.svc.cluster.local:8000",
    "input_cost_per_million": 0.20,
    "output_cost_per_million": 0.20
  }
}
```

`runtime_cost.estimated` is an attribution estimate based on the returned `usage` block. It is not a cloud billing source of truth.

## Benchmark

Run a controlled benchmark against the gateway. Keep the model, GPU class, concurrency, context length, prompt mix, cache state, and sampling parameters in the benchmark record; otherwise latency comparisons are not defensible.

```sh
k6 run -e GATEWAY_URL=https://ai.example.com -e MODEL=qwen2.5-7b-instruct \
  loadtest/chat-completions.js
```

Track TTFT, inter-token latency, E2E latency, prompt/output tokens per second, queue depth, KV-cache usage, error rate, and cost estimate per successful request.

## Repository layout

```text
app/gateway/          FastAPI OpenAI-compatible gateway
charts/vllm-runtime/  Primary vLLM Helm chart
deploy/               Kustomize base and optional platform integrations
gitops/argocd/        Argo CD application
loadtest/             k6 inference benchmark
docs/                 Architecture and operational decisions
```

## Upstream references

- [vLLM production stack](https://docs.vllm.ai/deployment/integrations/production-stack.html) documents Helm-based deployment, model-aware routing, and observability.
- [vLLM production metrics](https://docs.vllm.ai/en/latest/usage/metrics/) exposes the Prometheus metrics used by the scaling and dashboard design.
- [KServe](https://kserve.github.io/kserve/) provides the Kubernetes-native generative/predictive inference surface; its [Standard deployment mode](https://kserve.github.io/website/docs/admin-guide/kubernetes-deployment) supports optional KEDA custom-metric autoscaling.
- [KEDA's Prometheus scaler](https://keda.sh/docs/2.8/scalers/prometheus/) requires a query that evaluates to one scalar/vector element.

## Roadmap

1. Add Envoy AI Gateway policies, per-tenant API keys, rate limits, and JWT/OIDC authentication.
2. Add Ray Serve LLM as a multi-model/pipeline deployment profile.
3. Export a Grafana dashboard and SLO recording rules for TTFT, TPOT, queue depth, and error budget.
4. Introduce model revisions, canary analysis gates, and rollback based on live latency/error signals.
5. Add a reproducible benchmark report for a named GPU/model/version profile.

## Security note

No production secret, token, model licence acceptance, or cloud credential belongs in this repository. Supply them through the cluster's secret-management and workload-identity mechanism.
