# 30-second demo flow

The demo proves the end-to-end request path and the gateway's routing decisions without Kubernetes or a GPU.

## 1. Start the local runtime

```sh
docker compose -f deploy/local/docker-compose.yaml up --build
```

The first run downloads the Ollama image and `qwen2.5:1.5b`. The gateway starts only after the model pull completes.

## 2. Verify the public contract

```sh
curl http://localhost:8080/healthz
curl http://localhost:8080/v1/models
curl http://localhost:8080/v1/backends/health
```

Expected result: the gateway is healthy, the model is visible, and the backend health endpoint reports a probe-based score.

## 3. Call OpenAI-compatible chat completions

```sh
curl http://localhost:8080/v1/chat/completions \
  -H 'content-type: application/json' \
  -d '{
    "model": "qwen2.5:1.5b",
    "messages": [{"role": "user", "content": "Explain Kubernetes in one sentence."}],
    "max_tokens": 64
  }'
```

The response contains standard chat-completion data plus runtime attribution fields such as `selected_backend`, `fallback_used`, `health_score`, and `estimated_cost`.

## 4. Demonstrate routing modes

| Mode | Compose overlay | What to observe |
| --- | --- | --- |
| Canary | `docker-compose.canary.yaml` | `small-chat-canary` maps 90/10 using stable `X-Request-ID` affinity |
| Fallback | `docker-compose.fallback.yaml` | Closed primary port causes one retry on Llama |
| Health-aware | `docker-compose.fallback.yaml` | Low-score primary is skipped before inference |
| Cost-aware | `docker-compose.cost-aware.yaml` | Two healthy models; lower-cost Llama is selected |

Example cost-aware run:

```sh
docker compose \
  -f deploy/local/docker-compose.yaml \
  -f deploy/local/docker-compose.cost-aware.yaml \
  up --build --force-recreate

curl http://localhost:8080/v1/chat/completions \
  -H 'content-type: application/json' \
  -d '{"model":"small-chat","messages":[{"role":"user","content":"Hello"}]}'
```

Expected routing metadata includes `"routing_reason":"cost_aware"` and a concrete `selected_backend`.

## 5. Map the demo to production

The local gateway and request contract remain unchanged. Replace Ollama targets with the vLLM Helm runtime or KServe InferenceService, then add Prometheus, KEDA, OpenTelemetry, Grafana, and Argo CD from the production manifests. See [architecture.md](architecture.md) for the complete topology.
