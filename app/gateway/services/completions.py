"""Completion forwarding, shadow traffic, and decision recording."""

from __future__ import annotations

import asyncio
import time

import httpx

from app.gateway.config import ModelTarget
from app.gateway.decisions import DecisionRecord, DecisionStore
from app.gateway.metrics import (
    CHAT_COST,
    CHAT_DURATION,
    CHAT_FALLBACKS,
    CHAT_REQUESTS,
    CHAT_SHADOW,
    CHAT_SHADOW_DURATION,
)
from app.gateway.services.urls import chat_completions_url


async def run_shadow_request(
    client: httpx.AsyncClient,
    payload: dict[str, object],
    headers: dict[str, str],
    shadow_model: str,
    shadow_target: ModelTarget,
    timeout_seconds: float,
) -> tuple[str, float]:
    """Send a fire-and-forget copy of the request to the shadow backend."""
    shadow_headers = {**headers, "x-shadow-traffic": "true"}
    shadow_payload: dict[str, object] = {**payload, "model": shadow_model, "stream": False}
    if "max_tokens" not in shadow_payload:
        shadow_payload["max_tokens"] = 64
    started_at = time.monotonic()
    try:
        response = await client.post(
            chat_completions_url(shadow_target.url),
            json=shadow_payload,
            headers=shadow_headers,
            timeout=timeout_seconds,
        )
        response.raise_for_status()
        outcome = "success"
    except httpx.HTTPError:
        outcome = "error"
    duration_s = time.monotonic() - started_at
    CHAT_SHADOW.labels(shadow_backend=shadow_model, outcome=outcome).inc()
    CHAT_SHADOW_DURATION.labels(shadow_backend=shadow_model).observe(duration_s)
    return outcome, round(duration_s * 1000, 2)


async def complete_shadow_traffic(
    store: DecisionStore,
    request_id: str,
    client: httpx.AsyncClient,
    payload: dict[str, object],
    headers: dict[str, str],
    shadow_model: str,
    shadow_target: ModelTarget,
    timeout_seconds: float,
) -> None:
    outcome, duration_ms = await run_shadow_request(
        client, payload, headers, shadow_model, shadow_target, timeout_seconds
    )
    await store.patch_shadow(request_id, outcome=outcome, duration_ms=duration_ms)


def schedule_shadow_traffic(
    store: DecisionStore,
    client: httpx.AsyncClient,
    payload: dict[str, object],
    headers: dict[str, str],
    request_id: str,
    shadow_model: str,
    shadow_target: ModelTarget,
    timeout_seconds: float,
) -> None:
    asyncio.create_task(
        complete_shadow_traffic(
            store,
            request_id,
            client,
            payload,
            headers,
            shadow_model,
            shadow_target,
            timeout_seconds,
        )
    )


async def record_decision(
    store: DecisionStore,
    *,
    request_id: str,
    requested_model: str | None,
    selected_backend: str,
    routing_reason: str,
    fallback_used: bool,
    health_score: int | None,
    duration_ms: float,
    shadow_backend: str | None = None,
    estimated_cost: float | None = None,
    stream_outcome: str | None = None,
    stream_ttft_ms: float | None = None,
    control_plane_decision_id: str | None = None,
    approval_id: str | None = None,
    policy_bundle_id: str | None = None,
    policy_digest: str | None = None,
    request_digest: str | None = None,
    control_plane_version: str | None = None,
    runtime_version: str | None = None,
    enforcement_outcome: str | None = None,
) -> None:
    await store.put(
        DecisionRecord(
            request_id=request_id,
            requested_model=requested_model or "unknown",
            selected_backend=selected_backend,
            routing_reason=routing_reason,
            fallback_used=fallback_used,
            health_score=health_score,
            duration_ms=duration_ms,
            shadow_backend=shadow_backend,
            estimated_cost=estimated_cost,
            stream_outcome=stream_outcome,
            stream_ttft_ms=stream_ttft_ms,
            control_plane_decision_id=control_plane_decision_id,
            approval_id=approval_id,
            policy_bundle_id=policy_bundle_id,
            policy_digest=policy_digest,
            request_digest=request_digest,
            control_plane_version=control_plane_version,
            runtime_version=runtime_version,
            enforcement_outcome=enforcement_outcome,
        )
    )


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


def routing_reason(*, cost_rerouted: bool, health_rerouted: bool, fallback_used: bool) -> str:
    if cost_rerouted:
        return "cost_aware"
    if health_rerouted:
        return "health_score"
    if fallback_used:
        return "fallback"
    return "primary"


def observe_completion(
    *,
    requested_model: str | None,
    selected_backend: str,
    reason: str,
    success: bool,
    fallback_used: bool,
    duration_s: float,
    cost: float | None,
) -> None:
    """Emit Prometheus signals for a completion attempt, regardless of streaming mode."""
    CHAT_REQUESTS.labels(
        requested_model=requested_model or "unknown",
        selected_backend=selected_backend,
        routing_reason=reason,
        outcome="success" if success else "error",
    ).inc()
    CHAT_DURATION.labels(routing_reason=reason).observe(duration_s)
    if fallback_used:
        CHAT_FALLBACKS.labels(selected_backend=selected_backend, routing_reason=reason).inc()
    if cost:
        CHAT_COST.labels(selected_backend=selected_backend).inc(cost)
