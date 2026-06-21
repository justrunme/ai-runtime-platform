"""OpenAI-compatible gateway with deterministic model routing and cost attribution."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import time
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse
from starlette.background import BackgroundTask
from opentelemetry import trace
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, ConsoleSpanExporter
from pydantic import BaseModel, ConfigDict, Field, model_validator


class ModelTarget(BaseModel):
    """Endpoint and unit price for one served model."""

    url: str
    input_cost_per_million: float = Field(ge=0)
    output_cost_per_million: float = Field(ge=0)
    backend_name: str | None = None
    health_path: str = "/health"


class RouteTarget(BaseModel):
    """One weighted model target behind a public route alias."""

    model: str
    weight: int = Field(gt=0)


class ModelRoute(BaseModel):
    """Canary or failover policy for a public model alias."""

    targets: list[RouteTarget] = Field(default_factory=list)
    primary: str | None = None
    fallback: str | None = None

    @model_validator(mode="after")
    def has_valid_policy(self) -> "ModelRoute":
        is_canary = bool(self.targets)
        is_failover = self.primary is not None
        if is_canary == is_failover:
            raise ValueError("route must define either weighted targets or primary and fallback models")
        if is_canary and sum(target.weight for target in self.targets) != 100:
            raise ValueError("route target weights must total 100")
        if is_canary and self.fallback is not None:
            raise ValueError("weighted routes cannot define a fallback model")
        if is_failover and self.fallback is None:
            raise ValueError("failover routes require a fallback model")
        if is_failover and self.primary == self.fallback:
            raise ValueError("primary and fallback models must differ")
        return self

    def model_names(self) -> list[str]:
        if self.primary is not None:
            return [self.primary, self.fallback]  # type: ignore[list-item]
        return [target.model for target in self.targets]


class GatewaySettings(BaseModel):
    """Runtime configuration. MODEL_TARGETS is intentionally JSON for GitOps injection."""

    model_config = ConfigDict(frozen=True)
    model_targets: dict[str, ModelTarget]
    model_routes: dict[str, ModelRoute] = Field(default_factory=dict)
    timeout_seconds: float = Field(default=120.0, gt=0)
    health_interval_seconds: float = Field(default=15.0, gt=0)

    @model_validator(mode="after")
    def route_targets_exist(self) -> "GatewaySettings":
        missing = {
            model
            for route in self.model_routes.values()
            for model in route.model_names()
            if model not in self.model_targets
        }
        if missing:
            raise ValueError(f"route references unknown models: {', '.join(sorted(missing))}")
        return self

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
            ollama_models = os.getenv("OLLAMA_MODELS", os.getenv("OLLAMA_MODEL", "qwen2.5:1.5b"))
            ollama_url = ollama_base_url.rstrip("/")
            if not ollama_url.endswith("/v1"):
                ollama_url = f"{ollama_url}/v1"
            for ollama_model in (model.strip() for model in ollama_models.split(",") if model.strip()):
                model_targets.setdefault(
                    ollama_model,
                    {
                        "url": ollama_url,
                        "input_cost_per_million": float(os.getenv("OLLAMA_INPUT_COST_PER_MILLION", "0")),
                        "output_cost_per_million": float(os.getenv("OLLAMA_OUTPUT_COST_PER_MILLION", "0")),
                        "backend_name": f"ollama-{ollama_model}",
                        "health_path": "/",
                    },
                )
        raw_routes = os.getenv("MODEL_ROUTES")
        return cls.model_validate(
            {
                "model_targets": model_targets,
                "model_routes": json.loads(raw_routes) if raw_routes else {},
                "timeout_seconds": float(os.getenv("UPSTREAM_TIMEOUT_SECONDS", "120")),
                "health_interval_seconds": float(os.getenv("BACKEND_HEALTH_INTERVAL_SECONDS", "15")),
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


def backend_health_url(target: ModelTarget) -> str:
    """Build the health endpoint without assuming the OpenAI API base path."""
    base_url = target.url.rstrip("/")
    if base_url.endswith("/v1"):
        base_url = base_url.removesuffix("/v1")
    return f"{base_url}{target.health_path}"


@dataclass
class BackendHealth:
    available: bool | None = None
    latency_ms: float | None = None
    request_count: int = 0
    error_count: int = 0
    fallback_count: int = 0

    def score(self) -> int:
        if self.available is False:
            return 0
        latency_penalty = min((self.latency_ms or 0) / 20, 30)
        error_penalty = self.error_rate * 50
        fallback_penalty = self.fallback_rate * 20
        return round(max(0, 100 - latency_penalty - error_penalty - fallback_penalty))

    @property
    def error_rate(self) -> float:
        return self.error_count / self.request_count if self.request_count else 0.0

    @property
    def fallback_rate(self) -> float:
        return self.fallback_count / self.request_count if self.request_count else 0.0


class BackendHealthStore:
    """In-memory health signals for one gateway replica."""

    def __init__(self, settings: GatewaySettings, client: httpx.AsyncClient) -> None:
        self._settings = settings
        self._client = client
        self._health = {model: BackendHealth() for model in settings.model_targets}
        self._lock = asyncio.Lock()

    async def probe_all(self) -> None:
        await asyncio.gather(
            *(self.probe(model, target) for model, target in self._settings.model_targets.items())
        )

    async def probe(self, model: str, target: ModelTarget) -> None:
        started_at = time.monotonic()
        try:
            response = await self._client.get(backend_health_url(target), timeout=min(5, self._settings.timeout_seconds))
            available = response.is_success
        except httpx.RequestError:
            available = False
        latency_ms = round((time.monotonic() - started_at) * 1000, 2)
        async with self._lock:
            self._health[model].available = available
            self._health[model].latency_ms = latency_ms

    async def record_request(self, model: str, *, success: bool, fallback_used: bool = False) -> None:
        async with self._lock:
            health = self._health[model]
            health.request_count += 1
            health.error_count += int(not success)
            health.fallback_count += int(fallback_used)

    async def snapshot(self) -> list[dict[str, object]]:
        async with self._lock:
            return [
                {
                    "name": target.backend_name or model,
                    "model": model,
                    "status": "healthy" if health.available else "unhealthy" if health.available is False else "unknown",
                    "score": health.score(),
                    "latency_ms": health.latency_ms,
                    "error_rate": round(health.error_rate, 4),
                    "fallback_rate": round(health.fallback_rate, 4),
                }
                for model, target in self._settings.model_targets.items()
                for health in [self._health[model]]
            ]


async def health_probe_loop(store: BackendHealthStore, interval_seconds: float) -> None:
    while True:
        await store.probe_all()
        await asyncio.sleep(interval_seconds)


def select_route_target(route: ModelRoute, request_id: str, route_name: str) -> str:
    """Select a stable weighted target for a route and request identifier."""
    if not route.targets:
        raise ValueError("cannot select a weighted target from a failover route")
    bucket = int(hashlib.sha256(f"{route_name}:{request_id}".encode()).hexdigest(), 16) % 100
    upper_bound = 0
    for target in route.targets:
        upper_bound += target.weight
        if bucket < upper_bound:
            return target.model
    raise RuntimeError("validated route did not select a target")


def resolve_route(route: ModelRoute | None, request_id: str, route_name: str) -> tuple[str, str | None]:
    """Resolve a public route to primary and optional fallback model names."""
    if route is None:
        return route_name, None
    if route.primary is not None:
        return route.primary, route.fallback
    return select_route_target(route, request_id, route_name), None


async def post_completion_with_fallback(
    client: httpx.AsyncClient,
    payload: dict[str, object],
    headers: dict[str, str],
    primary_model: str,
    primary_target: ModelTarget,
    fallback_model: str | None = None,
    fallback_target: ModelTarget | None = None,
) -> tuple[httpx.Response, str, bool, list[str]]:
    """Call primary then retry one retryable failure against the fallback target."""
    attempts = [(primary_model, primary_target, False)]
    if fallback_model is not None and fallback_target is not None:
        attempts.append((fallback_model, fallback_target, True))

    failed_models: list[str] = []
    for index, (model, target, fallback_used) in enumerate(attempts):
        try:
            response = await client.post(
                chat_completions_url(target.url), json={**payload, "model": model}, headers=headers
            )
            response.raise_for_status()
            return response, model, fallback_used, failed_models
        except httpx.HTTPStatusError as error:
            if error.response.status_code < 500 or index == len(attempts) - 1:
                raise
            failed_models.append(model)
        except httpx.RequestError:
            if index == len(attempts) - 1:
                raise
            failed_models.append(model)

    raise RuntimeError("completion routing had no configured attempt")


def configure_tracing() -> None:
    provider = TracerProvider(resource=Resource.create({"service.name": "ai-runtime-gateway"}))
    provider.add_span_processor(BatchSpanProcessor(ConsoleSpanExporter()))
    trace.set_tracer_provider(provider)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = GatewaySettings.from_environment()
    app.state.settings = settings
    app.state.client = httpx.AsyncClient(timeout=settings.timeout_seconds)
    app.state.backend_health = BackendHealthStore(settings, app.state.client)
    health_task = asyncio.create_task(health_probe_loop(app.state.backend_health, settings.health_interval_seconds))
    try:
        yield
    finally:
        health_task.cancel()
        with suppress(asyncio.CancelledError):
            await health_task
        await app.state.client.aclose()


configure_tracing()
app = FastAPI(title="AI Runtime Gateway", version="0.1.0", lifespan=lifespan)
FastAPIInstrumentor.instrument_app(app)


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/v1/models")
async def list_models(request: Request) -> dict[str, object]:
    settings: GatewaySettings = request.app.state.settings
    return {
        "object": "list",
        "data": [
            {"id": model, "object": "model"}
            for model in [*settings.model_targets, *settings.model_routes]
        ],
    }


@app.get("/v1/routes")
async def list_routes(request: Request) -> dict[str, object]:
    """Expose public route aliases and weights without leaking backend addresses."""
    settings: GatewaySettings = request.app.state.settings
    return {
        "data": [
            {
                "id": route_name,
                "object": "model.route",
                "policy": "failover" if route.primary is not None else "weighted",
                "targets": [target.model_dump() for target in route.targets]
                if route.primary is None
                else [{"model": route.primary, "role": "primary"}, {"model": route.fallback, "role": "fallback"}],
            }
            for route_name, route in settings.model_routes.items()
        ]
    }


@app.get("/v1/backends/health")
async def backend_health(request: Request) -> dict[str, object]:
    return {"backends": await request.app.state.backend_health.snapshot()}


@app.post("/v1/chat/completions", response_model=None)
async def chat_completions(request: Request) -> JSONResponse | StreamingResponse:
    payload = await request.json()
    requested_model = payload.get("model")
    settings: GatewaySettings = request.app.state.settings
    request_id = request.headers.get("x-request-id", str(uuid.uuid4()))
    route = settings.model_routes.get(requested_model)
    model, fallback_model = resolve_route(route, request_id, requested_model)
    target = settings.model_targets.get(model)
    if target is None:
        raise HTTPException(status_code=404, detail=f"unknown model or route: {requested_model}")

    started_at = time.monotonic()
    headers = {"x-request-id": request_id}
    fallback_target = settings.model_targets.get(fallback_model) if fallback_model else None
    with trace.get_tracer(__name__).start_as_current_span("gen_ai.chat") as span:
        span.set_attribute("gen_ai.request.model", requested_model)
        if route:
            span.set_attribute("ai.runtime.route", requested_model)
        span.set_attribute("gen_ai.operation.name", "chat")
        try:
            if payload.get("stream"):
                upstream_url = chat_completions_url(target.url)
                upstream_request = request.app.state.client.build_request(
                    "POST", upstream_url, json={**payload, "model": model}, headers=headers
                )
                upstream = await request.app.state.client.send(upstream_request, stream=True)
                if upstream.status_code >= 500 and fallback_target is not None and fallback_model is not None:
                    await upstream.aclose()
                    model = fallback_model
                    upstream_request = request.app.state.client.build_request(
                        "POST",
                        chat_completions_url(fallback_target.url),
                        json={**payload, "model": model},
                        headers=headers,
                    )
                    upstream = await request.app.state.client.send(upstream_request, stream=True)
                    headers["x-fallback-used"] = "true"
                upstream.raise_for_status()
                headers["x-selected-backend"] = model
                return StreamingResponse(
                    upstream.aiter_bytes(),
                    status_code=upstream.status_code,
                    media_type=upstream.headers.get("content-type", "text/event-stream"),
                    headers=headers,
                    background=BackgroundTask(upstream.aclose),
                )
            upstream, model, fallback_used, failed_models = await post_completion_with_fallback(
                request.app.state.client,
                payload,
                headers,
                model,
                target,
                fallback_model,
                fallback_target,
            )
            for failed_model in failed_models:
                await request.app.state.backend_health.record_request(failed_model, success=False)
            await request.app.state.backend_health.record_request(
                model, success=True, fallback_used=fallback_used
            )
        except httpx.HTTPError as error:
            span.record_exception(error)
            raise HTTPException(status_code=502, detail="model backend unavailable") from error

        response = upstream.json()
        response["selected_backend"] = model
        response["fallback_used"] = fallback_used
        estimated_cost = request_cost(response.get("usage"), settings.model_targets[model])
        if estimated_cost is not None:
            response["runtime_cost"] = {"currency": "USD", "estimated": estimated_cost}
            span.set_attribute("gen_ai.usage.cost_usd", estimated_cost)
        span.set_attribute("gen_ai.server.time_to_last_byte_ms", round((time.monotonic() - started_at) * 1000))
        return JSONResponse(response, headers=headers)
