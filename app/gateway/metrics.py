"""Prometheus metrics for the Runtime Gateway."""

from __future__ import annotations

from prometheus_client import Counter, Histogram

CHAT_REQUESTS = Counter(
    "gateway_chat_requests_total",
    "Chat completion requests handled by the gateway, labelled by routing outcome.",
    ["requested_model", "selected_backend", "routing_reason", "outcome"],
)
CHAT_FALLBACKS = Counter(
    "gateway_chat_fallback_total",
    "Completions served by a non-primary backend after a reroute or failover.",
    ["selected_backend", "routing_reason"],
)
CHAT_DURATION = Histogram(
    "gateway_chat_duration_seconds",
    "End-to-end gateway latency for chat completions.",
    ["routing_reason"],
)
CHAT_COST = Counter(
    "gateway_chat_estimated_cost_usd_total",
    "Estimated USD cost attributed from upstream token usage.",
    ["selected_backend"],
)
CHAT_SHADOW = Counter(
    "gateway_chat_shadow_total",
    "Fire-and-forget shadow requests sent to a canary backend for comparison.",
    ["shadow_backend", "outcome"],
)
CHAT_SHADOW_DURATION = Histogram(
    "gateway_chat_shadow_duration_seconds",
    "Latency of shadow requests that do not affect the client response.",
    ["shadow_backend"],
)
