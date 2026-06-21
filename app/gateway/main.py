"""OpenAI-compatible gateway with deterministic model routing and cost attribution."""

from __future__ import annotations

import json
import os
import time
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse
from starlette.background import BackgroundTask
from opentelemetry import trace
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, ConsoleSpanExporter
from pydantic import BaseModel, ConfigDict, Field


class ModelTarget(BaseModel):
    """Endpoint and unit price for one served model."""

    url: str
    input_cost_per_million: float = Field(ge=0)
    output_cost_per_million: float = Field(ge=0)


class GatewaySettings(BaseModel):
    """Runtime configuration. MODEL_TARGETS is intentionally JSON for GitOps injection."""

    model_config = ConfigDict(frozen=True)
    model_targets: dict[str, ModelTarget]
    timeout_seconds: float = Field(default=120.0, gt=0)

    @classmethod
    def from_environment(cls) -> "GatewaySettings":
        default_targets = {
            "qwen2.5-7b-instruct": {
                "url": "http://vllm-qwen.ai-runtime.svc.cluster.local:8000",
                "input_cost_per_million": 0.20,
                "output_cost_per_million": 0.20,
            }
        }
        raw_targets = os.getenv("MODEL_TARGETS")
        model_targets = json.loads(raw_targets) if raw_targets else default_targets
        ollama_base_url = os.getenv("OLLAMA_BASE_URL")
        if ollama_base_url:
            ollama_model = os.getenv("OLLAMA_MODEL", "qwen2.5:1.5b")
            ollama_url = ollama_base_url.rstrip("/")
            if not ollama_url.endswith("/v1"):
                ollama_url = f"{ollama_url}/v1"
            model_targets.setdefault(
                ollama_model,
                {
                    "url": ollama_url,
                    "input_cost_per_million": float(os.getenv("OLLAMA_INPUT_COST_PER_MILLION", "0")),
                    "output_cost_per_million": float(os.getenv("OLLAMA_OUTPUT_COST_PER_MILLION", "0")),
                },
            )
        return cls.model_validate(
            {
                "model_targets": model_targets,
                "timeout_seconds": float(os.getenv("UPSTREAM_TIMEOUT_SECONDS", "120")),
            }
        )


def request_cost(usage: dict[str, int] | None, target: ModelTarget) -> float | None:
    """Return estimated USD cost from OpenAI usage data, or None when usage is absent."""
    if usage is None:
        return None
    input_tokens = usage.get("prompt_tokens", 0)
    output_tokens = usage.get("completion_tokens", 0)
    return round(
        (input_tokens * target.input_cost_per_million + output_tokens * target.output_cost_per_million)
        / 1_000_000,
        8,
    )


def chat_completions_url(base_url: str) -> str:
    """Build a chat-completions endpoint from an OpenAI-compatible base URL."""
    normalized = base_url.rstrip("/")
    suffix = "/chat/completions" if normalized.endswith("/v1") else "/v1/chat/completions"
    return f"{normalized}{suffix}"


def configure_tracing() -> None:
    provider = TracerProvider(resource=Resource.create({"service.name": "ai-runtime-gateway"}))
    provider.add_span_processor(BatchSpanProcessor(ConsoleSpanExporter()))
    trace.set_tracer_provider(provider)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = GatewaySettings.from_environment()
    app.state.settings = settings
    app.state.client = httpx.AsyncClient(timeout=settings.timeout_seconds)
    yield
    await app.state.client.aclose()


configure_tracing()
app = FastAPI(title="AI Runtime Gateway", version="0.1.0", lifespan=lifespan)
FastAPIInstrumentor.instrument_app(app)


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/v1/models")
async def list_models(request: Request) -> dict[str, object]:
    return {
        "object": "list",
        "data": [{"id": model, "object": "model"} for model in request.app.state.settings.model_targets],
    }


@app.post("/v1/chat/completions", response_model=None)
async def chat_completions(request: Request) -> JSONResponse | StreamingResponse:
    payload = await request.json()
    model = payload.get("model")
    settings: GatewaySettings = request.app.state.settings
    target = settings.model_targets.get(model)
    if target is None:
        raise HTTPException(status_code=404, detail=f"unknown model: {model}")

    request_id = request.headers.get("x-request-id", str(uuid.uuid4()))
    started_at = time.monotonic()
    headers = {"x-request-id": request_id}
    upstream_url = chat_completions_url(target.url)
    with trace.get_tracer(__name__).start_as_current_span("gen_ai.chat") as span:
        span.set_attribute("gen_ai.request.model", model)
        span.set_attribute("gen_ai.operation.name", "chat")
        try:
            if payload.get("stream"):
                upstream_request = request.app.state.client.build_request(
                    "POST", upstream_url, json=payload, headers=headers
                )
                upstream = await request.app.state.client.send(upstream_request, stream=True)
                return StreamingResponse(
                    upstream.aiter_bytes(),
                    status_code=upstream.status_code,
                    media_type=upstream.headers.get("content-type", "text/event-stream"),
                    headers=headers,
                    background=BackgroundTask(upstream.aclose),
                )
            upstream = await request.app.state.client.post(upstream_url, json=payload, headers=headers)
            upstream.raise_for_status()
        except httpx.HTTPError as error:
            span.record_exception(error)
            raise HTTPException(status_code=502, detail="model backend unavailable") from error

        response = upstream.json()
        estimated_cost = request_cost(response.get("usage"), target)
        if estimated_cost is not None:
            response["runtime_cost"] = {"currency": "USD", "estimated": estimated_cost}
            span.set_attribute("gen_ai.usage.cost_usd", estimated_cost)
        span.set_attribute("gen_ai.server.time_to_last_byte_ms", round((time.monotonic() - started_at) * 1000))
        return JSONResponse(response, headers=headers)
