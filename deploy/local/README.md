# Local CPU demo

This profile runs the same OpenAI-compatible gateway used in the Kubernetes path, but routes a local model to Ollama rather than vLLM. It is a functional demo path for a laptop; it is not representative of GPU throughput, batching, KV-cache behaviour, KEDA scaling, or production cost.

## Prerequisites

- Docker Desktop or Docker Engine with Compose v2.
- At least 8 GB RAM available to Docker and 6 GB free disk. The ARM64 Ollama runtime image is roughly 3 GB compressed, and the default `qwen2.5:1.5b` model adds roughly 1 GB; larger models need materially more memory and disk.

## Run the demo

From the repository root:

```sh
docker compose -f deploy/local/docker-compose.yaml up --build
```

The first run pulls the Ollama image and model. The `ollama-init` job completes only after the model is ready; the gateway waits for that job.

In a second terminal:

```sh
curl http://localhost:8080/healthz

curl http://localhost:8080/v1/chat/completions \
  -H 'content-type: application/json' \
  -d '{
    "model": "qwen2.5:1.5b",
    "messages": [{"role": "user", "content": "Explain Kubernetes in one sentence."}],
    "max_tokens": 64
  }'
```

Expected path:

```text
curl / OpenAI SDK -> gateway:8080 -> Ollama:11434/v1 -> local CPU model
```

After the first probe interval, inspect the backend health score:

```sh
curl http://localhost:8080/v1/backends/health
```

The response includes the original OpenAI-compatible body. `runtime_cost.estimated` is `0` by default for this local profile; set `OLLAMA_INPUT_COST_PER_MILLION` and `OLLAMA_OUTPUT_COST_PER_MILLION` only when demonstrating a hypothetical attribution rate.

## Change the model

Choose an Ollama model suitable for the available memory, then start the stack with it:

```sh
OLLAMA_MODEL=llama3.2:3b docker compose -f deploy/local/docker-compose.yaml up --build
```

The same exact model name must be sent in the `model` field. To remove the downloaded model volume:

```sh
docker compose -f deploy/local/docker-compose.yaml down -v
```

## Run the local canary profile

The canary overlay downloads `qwen2.5:1.5b` and `llama3.2:1b`, then adds the public `small-chat-canary` route: 90% primary and 10% canary.

```sh
docker compose \
  -f deploy/local/docker-compose.yaml \
  -f deploy/local/docker-compose.canary.yaml \
  up --build --force-recreate
```

Inspect the configured split and send a request through the alias:

```sh
curl http://localhost:8080/v1/routes

curl http://localhost:8080/v1/chat/completions \
  -H 'content-type: application/json' \
  -H 'x-request-id: canary-demo-001' \
  -d '{"model":"small-chat-canary","messages":[{"role":"user","content":"Hello"}]}'
```

The same `X-Request-ID` always resolves to the same model for this route. This makes retries stable; use distinct request IDs to observe the weighted distribution.

## Run the local fallback profile

The fallback overlay configures `small-chat` with Qwen as primary and Llama as fallback. Its primary URL deliberately points to a closed loopback port, so it proves that a connection failure produces a response from the fallback model.

```sh
docker compose \
  -f deploy/local/docker-compose.yaml \
  -f deploy/local/docker-compose.fallback.yaml \
  up --build --force-recreate

curl http://localhost:8080/v1/chat/completions \
  -H 'content-type: application/json' \
  -d '{"model":"small-chat","messages":[{"role":"user","content":"Hello"}]}'
```

The response must contain `"selected_backend":"llama3.2:1b"` and `"fallback_used":true`.

This profile also sets `min_health_score: 50`; after the probe loop marks the closed primary port unhealthy, the gateway skips it before attempting a completion. The response includes `"routing_reason":"health_score"`.

## Routing contract

When `OLLAMA_BASE_URL` is set, the gateway adds `OLLAMA_MODEL` (default: `qwen2.5:1.5b`) as a route to `<OLLAMA_BASE_URL>/v1`. An explicit entry in `MODEL_TARGETS` for the same model name wins, so GitOps configuration can override the local route deterministically.

Ollama's OpenAI-compatible endpoint supports `/v1/chat/completions`; this is why the gateway can use the same request contract in both demo and production profiles. See the [Ollama OpenAI compatibility documentation](https://docs.ollama.com/api/openai-compatibility).

## Docker DNS troubleshooting

Ollama downloads models from a Cloudflare R2 hostname. Some Docker Desktop DNS configurations fail to resolve that hostname even when the macOS host can resolve it. The Compose profile therefore sets public resolvers for the `ollama` service by default:

```sh
LOCAL_DEMO_DNS_PRIMARY=1.1.1.1 \
LOCAL_DEMO_DNS_SECONDARY=8.8.8.8 \
docker compose -f deploy/local/docker-compose.yaml up --build
```

For a corporate network, replace those values with the organisation-approved resolver addresses. After changing the Compose configuration, recreate the stack without deleting the model volume:

```sh
docker compose -f deploy/local/docker-compose.yaml up --build --force-recreate
```
