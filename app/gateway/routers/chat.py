"""OpenAI-compatible chat completions endpoint."""

from __future__ import annotations

import asyncio
import os
import time
import uuid

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, StreamingResponse
from opentelemetry import trace

from app.gateway.config import GatewaySettings
from app.gateway.errors import api_error, raise_api_error
from app.gateway.evaluations import (
    build_evaluation_payload,
    response_evaluation_enabled,
    submit_response_evaluation,
)
from app.gateway.evidence import (
    EnforcementEvidence,
    apply_evidence_headers,
    evidence_from_governance,
)
from app.gateway.governance import GovernanceConfig, enforce_governance
from app.gateway.services.completions import (
    observe_completion,
    post_completion_with_fallback,
    record_decision,
    routing_reason,
    schedule_shadow_traffic,
)
from app.gateway.services.routing import (
    NoHealthyBackendError,
    resolve_route,
    resolve_shadow_backend,
    select_cost_aware_backend,
    select_health_aware_backend,
)
from app.gateway.services.urls import chat_completions_url, request_cost
from app.gateway.streaming import observe_upstream_stream, stream_headers
from app.gateway.tenant import TenantAttributionBackend

router = APIRouter(tags=["chat"])


@router.post("/v1/chat/completions", response_model=None)
async def chat_completions(request: Request) -> JSONResponse | StreamingResponse:
    try:
        payload = await request.json()
    except Exception as error:  # noqa: BLE001 - return OpenAI-compatible validation envelope
        raise api_error(
            422,
            code="invalid_request",
            message="request validation failed",
            error_type="invalid_request_error",
            request_id=request.headers.get("x-request-id"),
        ) from error
    if not isinstance(payload, dict):
        raise_api_error(
            422,
            code="invalid_request",
            message="request body must be a JSON object",
            error_type="invalid_request_error",
            request_id=request.headers.get("x-request-id"),
        )
    requested_model = payload.get("model")
    settings: GatewaySettings = request.app.state.settings
    request_id = request.headers.get("x-request-id", str(uuid.uuid4()))
    governance: GovernanceConfig | None = request.app.state.governance
    tenant_store: TenantAttributionBackend | None = getattr(
        request.app.state, "tenant_attribution", None
    )
    requests_last_minute: int | None = None
    tokens_today: int | None = None
    if tenant_store is not None:
        team = tenant_store.resolve_team(request)
        input_tokens, output_tokens = 0, 0
        messages = payload.get("messages")
        if isinstance(messages, list):
            for message in messages:
                if isinstance(message, dict) and isinstance(message.get("content"), str):
                    input_tokens += max(1, len(message["content"]) // 4)
        output_tokens = int(payload.get("max_tokens") or min(input_tokens or 1, 512))
        await tenant_store.record_request(
            team, input_tokens=input_tokens or 1, output_tokens=output_tokens
        )
        requests_last_minute, tokens_today = await tenant_store.usage_snapshot(team)
    evidence: EnforcementEvidence | None = None
    if governance is not None:
        evidence = evidence_from_governance(
            await enforce_governance(
                request.app.state.client,
                governance,
                request,
                payload,
                settings.model_targets,
                requests_last_minute=requests_last_minute,
                tokens_today=tokens_today,
            ),
            enforcement_outcome="executed",
        )
        runtime_config = getattr(request.app.state, "runtime_config", None)
        if runtime_config is not None and evidence is not None:
            runtime_config.policy.observe(
                bundle_id=evidence.policy_bundle_id,
                digest=evidence.policy_digest,
            )
    else:
        evidence = evidence_from_governance(None, enforcement_outcome="executed")
    route = settings.model_routes.get(requested_model)
    model, fallback_model = resolve_route(route, request_id, requested_model)
    try:
        if route and route.routing_policy:
            model, cost_rerouted = await select_cost_aware_backend(
                route,
                model,
                fallback_model,
                request.app.state.backend_health,
                settings.model_targets,
            )
            health_rerouted = False
        else:
            model, health_rerouted = await select_health_aware_backend(
                route, model, fallback_model, request.app.state.backend_health
            )
            cost_rerouted = False
    except NoHealthyBackendError as error:
        raise api_error(
            503,
            code="no_healthy_backend",
            message="no healthy backend for route",
            error_type="api_error",
        ) from error
    if health_rerouted or cost_rerouted:
        fallback_model = None
    target = settings.model_targets.get(model)
    if target is None:
        raise_api_error(
            404,
            code="model_not_found",
            message=f"unknown model or route: {requested_model}",
            error_type="invalid_request_error",
            request_id=request_id,
        )

    shadow_model = resolve_shadow_backend(route, model)
    shadow_target = settings.model_targets.get(shadow_model) if shadow_model else None

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
                stream_fallback_used = False
                upstream_url = chat_completions_url(target.url)
                upstream_request = request.app.state.client.build_request(
                    "POST", upstream_url, json={**payload, "model": model}, headers=headers
                )
                upstream = await request.app.state.client.send(upstream_request, stream=True)
                if (
                    upstream.status_code >= 500
                    and fallback_target is not None
                    and fallback_model is not None
                ):
                    await upstream.aclose()
                    await request.app.state.backend_health.record_request(model, success=False)
                    model = fallback_model
                    stream_fallback_used = True
                    upstream_request = request.app.state.client.build_request(
                        "POST",
                        chat_completions_url(fallback_target.url),
                        json={**payload, "model": model},
                        headers=headers,
                    )
                    upstream = await request.app.state.client.send(upstream_request, stream=True)
                    headers["x-fallback-used"] = "true"
                upstream.raise_for_status()
                fallback_used = stream_fallback_used or health_rerouted or cost_rerouted
                reason = routing_reason(
                    cost_rerouted=cost_rerouted,
                    health_rerouted=health_rerouted,
                    fallback_used=stream_fallback_used,
                )
                response_headers = apply_evidence_headers(
                    stream_headers(headers, selected_backend=model, fallback_used=fallback_used),
                    evidence,
                )
                span.set_attribute("ai.runtime.routing_reason", reason)
                span.set_attribute("ai.runtime.selected_backend", model)
                if evidence and evidence.control_plane_decision_id:
                    span.set_attribute(
                        "ai.runtime.control_plane_decision_id",
                        evidence.control_plane_decision_id,
                    )
                if shadow_model and shadow_target:
                    schedule_shadow_traffic(
                        request.app.state.decision_store,
                        request.app.state.client,
                        payload,
                        response_headers,
                        request_id,
                        shadow_model,
                        shadow_target,
                        settings.timeout_seconds,
                    )
                    response_headers["x-shadow-backend"] = shadow_model
                    span.set_attribute("ai.runtime.shadow_backend", shadow_model)

                selected_model = model
                health_store = request.app.state.backend_health
                decision_store = request.app.state.decision_store
                evidence_fields = evidence.as_decision_fields() if evidence else {}

                async def _finalize_stream(observation) -> None:
                    success = observation.outcome == "success"
                    await health_store.record_request(
                        selected_model, success=success, fallback_used=fallback_used
                    )
                    observe_completion(
                        requested_model=requested_model,
                        selected_backend=selected_model,
                        reason=reason,
                        success=success,
                        fallback_used=fallback_used,
                        duration_s=observation.duration_ms / 1000,
                        cost=None,
                    )
                    span.set_attribute("ai.runtime.stream_outcome", observation.outcome)
                    if observation.ttft_ms is not None:
                        span.set_attribute("ai.runtime.stream_ttft_ms", observation.ttft_ms)
                    await record_decision(
                        decision_store,
                        request_id=request_id,
                        requested_model=requested_model,
                        selected_backend=selected_model,
                        routing_reason=reason,
                        fallback_used=fallback_used,
                        health_score=(await health_store.routing_signal(selected_model))[0],
                        duration_ms=observation.duration_ms,
                        shadow_backend=shadow_model,
                        stream_outcome=observation.outcome,
                        stream_ttft_ms=observation.ttft_ms,
                        **evidence_fields,
                        enforcement_outcome=(
                            "executed" if success else f"stream_{observation.outcome}"
                        ),
                    )

                return StreamingResponse(
                    observe_upstream_stream(
                        upstream,
                        selected_backend=selected_model,
                        on_complete=_finalize_stream,
                    ),
                    status_code=upstream.status_code,
                    media_type=upstream.headers.get("content-type", "text/event-stream"),
                    headers=response_headers,
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
                model, success=True, fallback_used=fallback_used or health_rerouted or cost_rerouted
            )
        except httpx.HTTPError as error:
            span.record_exception(error)
            await request.app.state.backend_health.record_request(model, success=False)
            reason = routing_reason(
                cost_rerouted=cost_rerouted,
                health_rerouted=health_rerouted,
                fallback_used=False,
            )
            observe_completion(
                requested_model=requested_model,
                selected_backend=model,
                reason=reason,
                success=False,
                fallback_used=False,
                duration_s=time.monotonic() - started_at,
                cost=None,
            )
            await record_decision(
                request.app.state.decision_store,
                request_id=request_id,
                requested_model=requested_model,
                selected_backend=model,
                routing_reason=reason,
                fallback_used=False,
                health_score=(await request.app.state.backend_health.routing_signal(model))[0],
                duration_ms=round((time.monotonic() - started_at) * 1000, 2),
                **(evidence.as_decision_fields() if evidence else {}),
                enforcement_outcome="upstream_error",
            )
            raise api_error(
                502,
                code="upstream_unavailable",
                message="model backend unavailable",
                error_type="api_error",
                request_id=request_id,
            ) from error

        response = upstream.json()
        reason = routing_reason(
            cost_rerouted=cost_rerouted,
            health_rerouted=health_rerouted,
            fallback_used=fallback_used,
        )
        fallback_flag = fallback_used or health_rerouted or cost_rerouted
        health_score, _, _ = await request.app.state.backend_health.routing_signal(model)
        estimated_cost = request_cost(response.get("usage"), settings.model_targets[model])
        headers["x-selected-backend"] = model
        headers["x-routing-reason"] = reason
        headers["x-fallback-used"] = "true" if fallback_flag else "false"
        headers["x-ai-health-score"] = str(health_score)
        apply_evidence_headers(headers, evidence)
        if estimated_cost is not None:
            headers["x-ai-estimated-cost-usd"] = str(estimated_cost)
            span.set_attribute("gen_ai.usage.cost_usd", estimated_cost)
        # Optional legacy body embedding for older demos/clients.
        if os.getenv("GATEWAY_EMBED_ROUTING_METADATA", "").strip().lower() in {
            "1",
            "true",
            "yes",
        }:
            response["selected_backend"] = model
            response["fallback_used"] = fallback_flag
            response["routing_reason"] = reason
            response["health_score"] = health_score
            if estimated_cost is not None:
                response["estimated_cost"] = estimated_cost
                response["runtime_cost"] = {"currency": "USD", "estimated": estimated_cost}
        span.set_attribute("ai.runtime.routing_reason", reason)
        span.set_attribute("ai.runtime.selected_backend", model)
        span.set_attribute(
            "gen_ai.server.time_to_last_byte_ms", round((time.monotonic() - started_at) * 1000)
        )
        observe_completion(
            requested_model=requested_model,
            selected_backend=model,
            reason=reason,
            success=True,
            fallback_used=fallback_flag,
            duration_s=time.monotonic() - started_at,
            cost=estimated_cost,
        )
        if shadow_model and shadow_target:
            schedule_shadow_traffic(
                request.app.state.decision_store,
                request.app.state.client,
                payload,
                headers,
                request_id,
                shadow_model,
                shadow_target,
                settings.timeout_seconds,
            )
            headers["x-shadow-backend"] = shadow_model
            span.set_attribute("ai.runtime.shadow_backend", shadow_model)
        await record_decision(
            request.app.state.decision_store,
            request_id=request_id,
            requested_model=requested_model,
            selected_backend=model,
            routing_reason=reason,
            fallback_used=fallback_flag,
            health_score=health_score,
            duration_ms=round((time.monotonic() - started_at) * 1000, 2),
            shadow_backend=shadow_model,
            estimated_cost=estimated_cost,
            **(evidence.as_decision_fields() if evidence else {}),
            enforcement_outcome="executed",
        )
        if governance is not None and response_evaluation_enabled():
            team = request.headers.get("x-ai-team", governance.default_team)
            eval_payload = build_evaluation_payload(
                team=team,
                model=str(requested_model or model),
                request_id=request_id,
                chat_payload=payload,
                completion=response,
                latency_ms=round((time.monotonic() - started_at) * 1000, 2),
                cost_usd=estimated_cost,
            )
            asyncio.create_task(
                submit_response_evaluation(
                    request.app.state.client,
                    governance,
                    eval_payload,
                )
            )
        return JSONResponse(response, headers=headers)
