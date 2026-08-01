"""Route resolution and health/cost-aware backend selection."""

from __future__ import annotations

import hashlib

from app.gateway.config import ModelRoute, ModelTarget
from app.gateway.stores.health import HealthStore


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


def resolve_route(
    route: ModelRoute | None, request_id: str, route_name: str
) -> tuple[str, str | None]:
    """Resolve a public route to primary and optional fallback model names."""
    if route is None:
        return route_name, None
    if route.primary is not None:
        return route.primary, route.fallback
    return select_route_target(route, request_id, route_name), None


def resolve_shadow_backend(route: ModelRoute | None, selected_model: str) -> str | None:
    """Mirror stable traffic to the configured canary without serving its response."""
    if route is None or route.shadow is None or route.shadow == selected_model:
        return None
    return route.shadow


class NoHealthyBackendError(Exception):
    """Raised when a health-aware route has no eligible backend."""


async def select_health_aware_backend(
    route: ModelRoute | None,
    primary_model: str,
    fallback_model: str | None,
    health_store: HealthStore,
    circuit_breaker: object | None = None,
) -> tuple[str, bool]:
    """Skip an unhealthy primary before issuing an inference request."""

    def circuit_allows(model: str) -> bool:
        if circuit_breaker is None:
            return True
        allow = getattr(circuit_breaker, "allow", None)
        return True if allow is None else bool(allow(model))

    if route is None or route.min_health_score is None:
        if circuit_allows(primary_model):
            return primary_model, False
        if fallback_model is not None and circuit_allows(fallback_model):
            return fallback_model, True
        raise NoHealthyBackendError("no backend available (circuit open)")
    if circuit_allows(primary_model) and await health_store.meets_score(
        primary_model, route.min_health_score
    ):
        return primary_model, False
    if (
        fallback_model is not None
        and circuit_allows(fallback_model)
        and await health_store.meets_score(fallback_model, route.min_health_score)
    ):
        return fallback_model, True
    raise NoHealthyBackendError("no backend meets the route health threshold")


async def select_cost_aware_backend(
    route: ModelRoute | None,
    primary_model: str,
    fallback_model: str | None,
    health_store: HealthStore,
    model_targets: dict[str, ModelTarget],
) -> tuple[str, bool]:
    """Choose the best healthy failover target using health, latency, and unit cost."""
    if route is None or route.routing_policy is None or fallback_model is None:
        return primary_model, False
    candidates = [primary_model, fallback_model]
    signals = {model: await health_store.routing_signal(model) for model in candidates}
    threshold = route.min_health_score or 0
    eligible = [
        model
        for model, (health_score, _, available) in signals.items()
        if available is not False and health_score >= threshold
    ]
    if not eligible:
        raise NoHealthyBackendError("no backend meets the route health threshold")
    if len(eligible) == 1:
        selected = eligible[0]
        return selected, selected != primary_model

    costs = {
        model: model_targets[model].input_cost_per_million
        + model_targets[model].output_cost_per_million
        for model in eligible
    }
    latencies = {model: signals[model][1] or float("inf") for model in eligible}
    min_cost = min(costs.values())
    min_latency = min(latencies.values())
    weights = route.routing_policy.weights

    def score(model: str) -> float:
        health_score, latency_ms, _ = signals[model]
        latency_component = (
            min_latency / (latency_ms or float("inf")) if min_latency != float("inf") else 1
        )
        cost_component = min_cost / costs[model] if costs[model] else 1
        return (
            weights.health * health_score / 100
            + weights.latency * latency_component
            + weights.cost * cost_component
        )

    selected = max(eligible, key=lambda model: (score(model), model == primary_model))
    return selected, selected != primary_model
