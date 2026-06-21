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

## Routing contract

When `OLLAMA_BASE_URL` is set, the gateway adds `OLLAMA_MODEL` (default: `qwen2.5:1.5b`) as a route to `<OLLAMA_BASE_URL>/v1`. An explicit entry in `MODEL_TARGETS` for the same model name wins, so GitOps configuration can override the local route deterministically.

Ollama's OpenAI-compatible endpoint supports `/v1/chat/completions`; this is why the gateway can use the same request contract in both demo and production profiles. See the [Ollama OpenAI compatibility documentation](https://docs.ollama.com/api/openai-compatibility).
