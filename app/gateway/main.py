"""OpenAI-compatible gateway with deterministic model routing and cost attribution."""

from __future__ import annotations

import asyncio
import os
import signal
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress

import httpx
from fastapi import FastAPI
from fastapi.responses import Response
from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, ConsoleSpanExporter, SpanExporter
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

from app.gateway.admission import TenantAdmissionController
from app.gateway.auth import PUBLIC_PATHS, install_authentication, request_is_authorized
from app.gateway.config import (
    GatewaySettings,
    ModelRoute,
    ModelTarget,
    RouteTarget,
    RoutingPolicy,
    RoutingWeights,
)
from app.gateway.decisions import create_decision_store
from app.gateway.errors import register_exception_handlers
from app.gateway.governance import GovernanceConfig
from app.gateway.metrics import (
    CHAT_COST,
    CHAT_DURATION,
    CHAT_FALLBACKS,
    CHAT_REQUESTS,
    CHAT_SHADOW,
    CHAT_SHADOW_DURATION,
)
from app.gateway.resilience import (
    CircuitBreaker,
    DrainState,
    GlobalAdmissionController,
    RetryBudget,
)
from app.gateway.routers import chat as chat_router
from app.gateway.routers import decisions as decisions_router
from app.gateway.routers import mcp as mcp_router
from app.gateway.routers import models as models_router
from app.gateway.routers import runtime as runtime_router
from app.gateway.routers.health import router as health_router
from app.gateway.runtime_config import RuntimeConfigState
from app.gateway.services.completions import (
    complete_shadow_traffic,
    observe_completion,
    post_completion_with_fallback,
    record_decision,
    routing_reason,
    run_shadow_request,
    schedule_shadow_traffic,
)
from app.gateway.services.routing import (
    NoHealthyBackendError,
    resolve_route,
    resolve_shadow_backend,
    select_cost_aware_backend,
    select_health_aware_backend,
    select_route_target,
)
from app.gateway.services.urls import backend_health_url, chat_completions_url, request_cost
from app.gateway.stores.health import (
    BackendHealth,
    BackendHealthStore,
    HealthStore,
    RedisHealthStore,
    create_health_store,
    health_probe_loop,
)
from app.gateway.tenant import create_tenant_store
from app.gateway.tenant_policy import load_tenant_policy_bundle
from app.gateway.usage_events import UsageEventEmitter

# Re-exports for existing tests and importers.
__all__ = [
    "BackendHealth",
    "BackendHealthStore",
    "CHAT_COST",
    "CHAT_DURATION",
    "CHAT_FALLBACKS",
    "CHAT_REQUESTS",
    "CHAT_SHADOW",
    "CHAT_SHADOW_DURATION",
    "GatewaySettings",
    "HealthStore",
    "ModelRoute",
    "ModelTarget",
    "NoHealthyBackendError",
    "PUBLIC_PATHS",
    "RedisHealthStore",
    "RouteTarget",
    "RoutingPolicy",
    "RoutingWeights",
    "app",
    "backend_health_url",
    "chat_completions_url",
    "complete_shadow_traffic",
    "create_decision_store",
    "create_health_store",
    "health_probe_loop",
    "observe_completion",
    "post_completion_with_fallback",
    "record_decision",
    "request_cost",
    "request_is_authorized",
    "resolve_route",
    "resolve_shadow_backend",
    "routing_reason",
    "run_shadow_request",
    "schedule_shadow_traffic",
    "select_cost_aware_backend",
    "select_health_aware_backend",
    "select_route_target",
]


def build_span_exporter() -> SpanExporter:
    """Export to the OTLP collector when configured, otherwise log spans to the console."""
    if os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT") or os.getenv("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT"):
        return OTLPSpanExporter()
    return ConsoleSpanExporter()


def configure_tracing() -> None:
    if os.getenv("OTEL_SDK_DISABLED", "").strip().lower() in {"1", "true", "yes"}:
        return
    service_name = os.getenv("OTEL_SERVICE_NAME", "ai-runtime-gateway")
    provider = TracerProvider(resource=Resource.create({"service.name": service_name}))
    provider.add_span_processor(BatchSpanProcessor(build_span_exporter()))
    trace.set_tracer_provider(provider)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = GatewaySettings.from_environment()
    app.state.settings = settings
    app.state.runtime_config = RuntimeConfigState.from_settings(settings)
    app.state.admission = TenantAdmissionController(load_tenant_policy_bundle())
    app.state.global_admission = GlobalAdmissionController()
    app.state.circuit_breaker = CircuitBreaker()
    app.state.retry_budget = RetryBudget()
    app.state.drain = DrainState()
    app.state.client = httpx.AsyncClient(timeout=settings.timeout_seconds)
    app.state.usage_events = UsageEventEmitter(app.state.client)
    app.state.backend_health = create_health_store(settings, app.state.client)
    app.state.decision_store = create_decision_store(settings.redis_url)
    app.state.governance = GovernanceConfig.from_environment()
    app.state.tenant_attribution = create_tenant_store(settings.redis_url)
    health_task = asyncio.create_task(
        health_probe_loop(app.state.backend_health, settings.health_interval_seconds)
    )
    loop = asyncio.get_running_loop()

    def _begin_drain(*_args: object) -> None:
        app.state.drain.begin()

    with suppress(NotImplementedError):
        # Windows / restricted sandboxes may not support add_signal_handler.
        loop.add_signal_handler(signal.SIGTERM, _begin_drain)
        loop.add_signal_handler(signal.SIGINT, _begin_drain)
    try:
        yield
    finally:
        app.state.drain.begin()
        health_task.cancel()
        with suppress(asyncio.CancelledError):
            await health_task
        await app.state.backend_health.aclose()
        tenant_store = getattr(app.state, "tenant_attribution", None)
        if tenant_store is not None:
            await tenant_store.aclose()
        await app.state.decision_store.aclose()
        await app.state.client.aclose()


configure_tracing()
app = FastAPI(title="AI Runtime Gateway", version="2.0.1", lifespan=lifespan)
FastAPIInstrumentor.instrument_app(app)
register_exception_handlers(app)
install_authentication(app)

app.include_router(health_router)
app.include_router(models_router.router)
app.include_router(decisions_router.router)
app.include_router(mcp_router.router)
app.include_router(chat_router.router)
app.include_router(runtime_router.router)


@app.get("/metrics")
async def metrics() -> Response:
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)
