"""Usage/cost event emission for Control Plane chargeback (not a billing engine)."""

from __future__ import annotations

import asyncio
import json
import os
import uuid
from collections import deque
from contextlib import suppress
from dataclasses import asdict, dataclass
from typing import Any

import httpx
from opentelemetry import trace
from prometheus_client import Counter

USAGE_EVENTS = Counter(
    "gateway_usage_events_total",
    "Usage events emitted by the runtime gateway.",
    ["outcome", "sink"],
)
USAGE_EVENTS_DROPPED = Counter(
    "gateway_usage_events_dropped_total",
    "Usage events dropped due to local buffer overflow.",
)


@dataclass(frozen=True)
class UsageEvent:
    event_id: str
    tenant_id: str
    request_id: str
    decision_id: str | None
    model: str
    backend: str
    input_tokens: int
    output_tokens: int
    estimated_cost_usd: float | None
    gpu_seconds: float | None
    ttft_ms: float | None
    duration_ms: float
    outcome: str

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def usage_webhook_url() -> str | None:
    url = os.getenv("USAGE_EVENTS_WEBHOOK_URL", "").strip()
    return url or None


def usage_buffer_max() -> int:
    raw = os.getenv("USAGE_EVENTS_BUFFER_MAX", "1000").strip()
    return max(1, int(raw or "1000"))


class UsageEventEmitter:
    """Bounded local buffer + optional webhook delivery; always attaches OTEL attributes."""

    def __init__(self, client: httpx.AsyncClient | None = None) -> None:
        self._client = client
        self._buffer: deque[UsageEvent] = deque()
        self._max = usage_buffer_max()
        self._lock = asyncio.Lock()

    def build(
        self,
        *,
        tenant_id: str,
        request_id: str,
        decision_id: str | None,
        model: str,
        backend: str,
        input_tokens: int,
        output_tokens: int,
        estimated_cost_usd: float | None,
        duration_ms: float,
        outcome: str,
        ttft_ms: float | None = None,
        gpu_seconds: float | None = None,
    ) -> UsageEvent:
        return UsageEvent(
            event_id=f"evt_{uuid.uuid4().hex}",
            tenant_id=tenant_id,
            request_id=request_id,
            decision_id=decision_id,
            model=model,
            backend=backend,
            input_tokens=max(0, int(input_tokens)),
            output_tokens=max(0, int(output_tokens)),
            estimated_cost_usd=estimated_cost_usd,
            gpu_seconds=gpu_seconds,
            ttft_ms=ttft_ms,
            duration_ms=duration_ms,
            outcome=outcome,
        )

    def _annotate_span(self, event: UsageEvent) -> None:
        span = trace.get_current_span()
        if span is None or not span.is_recording():
            USAGE_EVENTS.labels(outcome=event.outcome, sink="otel_skip").inc()
            return
        payload = event.as_dict()
        for key, value in payload.items():
            if value is None:
                continue
            span.set_attribute(f"ai.runtime.usage.{key}", value)
        USAGE_EVENTS.labels(outcome=event.outcome, sink="otel").inc()

    async def emit(self, event: UsageEvent) -> None:
        self._annotate_span(event)
        webhook = usage_webhook_url()
        if not webhook:
            return
        async with self._lock:
            if len(self._buffer) >= self._max:
                self._buffer.popleft()
                USAGE_EVENTS_DROPPED.inc()
            self._buffer.append(event)
        asyncio.create_task(self._flush_one(webhook, event))

    async def _flush_one(self, webhook: str, event: UsageEvent) -> None:
        if self._client is None:
            return
        try:
            response = await self._client.post(
                webhook,
                content=json.dumps(event.as_dict(), separators=(",", ":")),
                headers={
                    "Content-Type": "application/json",
                    "Idempotency-Key": event.event_id,
                    "X-Event-Id": event.event_id,
                },
                timeout=2.0,
            )
            response.raise_for_status()
            USAGE_EVENTS.labels(outcome=event.outcome, sink="webhook").inc()
            async with self._lock:
                with suppress(ValueError):
                    self._buffer.remove(event)
        except Exception:  # noqa: BLE001 - bounded buffer retains for later drop metrics
            USAGE_EVENTS.labels(outcome=event.outcome, sink="webhook_error").inc()


def tokens_from_usage(usage: dict[str, Any] | None) -> tuple[int, int]:
    if not isinstance(usage, dict):
        return 0, 0
    return int(usage.get("prompt_tokens") or 0), int(usage.get("completion_tokens") or 0)


def estimate_gpu_seconds(duration_ms: float) -> float:
    # Coarse proxy until backends export true GPU time.
    return round(max(0.0, duration_ms) / 1000.0, 4)
