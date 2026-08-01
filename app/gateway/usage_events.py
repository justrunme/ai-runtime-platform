"""Usage/cost event emission for Control Plane chargeback (not a billing engine)."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import time
from collections import deque
from contextlib import suppress
from dataclasses import dataclass, field
from typing import Any

import httpx
from opentelemetry import trace
from prometheus_client import Counter, Gauge, Histogram

USAGE_EVENTS = Counter(
    "gateway_usage_events_total",
    "Usage events emitted by the runtime gateway.",
    ["outcome", "sink"],
)
USAGE_EVENTS_DROPPED = Counter(
    "gateway_usage_events_dropped_total",
    "Usage events dropped due to local buffer overflow or shutdown.",
    ["reason"],
)
USAGE_BUFFER_DEPTH = Gauge(
    "gateway_usage_events_buffer_depth",
    "Pending usage events waiting for webhook delivery.",
)
USAGE_DELIVERY_LAG = Histogram(
    "gateway_usage_events_delivery_lag_seconds",
    "Seconds from enqueue to successful webhook delivery.",
    buckets=(0.01, 0.05, 0.1, 0.25, 0.5, 1, 2, 5, 15, 60),
)


@dataclass
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
    enqueued_at: float = 0.0
    attempts: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "tenant_id": self.tenant_id,
            "request_id": self.request_id,
            "decision_id": self.decision_id,
            "model": self.model,
            "backend": self.backend,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "estimated_cost_usd": self.estimated_cost_usd,
            "gpu_seconds": self.gpu_seconds,
            "ttft_ms": self.ttft_ms,
            "duration_ms": self.duration_ms,
            "outcome": self.outcome,
        }


@dataclass
class _Pending:
    event: UsageEvent
    next_attempt_at: float = field(default_factory=time.monotonic)


def usage_webhook_url() -> str | None:
    url = os.getenv("USAGE_EVENTS_WEBHOOK_URL", "").strip()
    return url or None


def usage_buffer_max() -> int:
    raw = os.getenv("USAGE_EVENTS_BUFFER_MAX", "1000").strip()
    return max(1, int(raw or "1000"))


def usage_max_attempts() -> int:
    raw = os.getenv("USAGE_EVENTS_MAX_ATTEMPTS", "8").strip()
    return max(1, int(raw or "8"))


def deterministic_event_id(*, request_id: str, outcome: str, backend: str) -> str:
    digest = hashlib.sha256(f"{request_id}:{outcome}:{backend}".encode()).hexdigest()[:24]
    return f"evt_{digest}"


class UsageEventEmitter:
    """Bounded queue with background retry worker and shutdown drain."""

    def __init__(self, client: httpx.AsyncClient | None = None) -> None:
        self._client = client
        self._pending: deque[_Pending] = deque()
        self._max = usage_buffer_max()
        self._max_attempts = usage_max_attempts()
        self._lock = asyncio.Lock()
        self._wake = asyncio.Event()
        self._accepting = True
        self._worker: asyncio.Task[None] | None = None

    def start(self) -> None:
        if self._worker is None or self._worker.done():
            self._accepting = True
            self._worker = asyncio.create_task(self._run_worker(), name="usage-events-worker")

    async def aclose(self, *, drain_timeout_seconds: float = 5.0) -> None:
        self._accepting = False
        self._wake.set()
        if self._worker is None:
            return
        try:
            await asyncio.wait_for(self._drain_until_empty(), timeout=drain_timeout_seconds)
        except TimeoutError:
            async with self._lock:
                dropped = len(self._pending)
                self._pending.clear()
            if dropped:
                USAGE_EVENTS_DROPPED.labels(reason="shutdown_timeout").inc(dropped)
                USAGE_BUFFER_DEPTH.set(0)
        self._worker.cancel()
        with suppress(asyncio.CancelledError):
            await self._worker
        self._worker = None

    async def _drain_until_empty(self) -> None:
        while True:
            async with self._lock:
                if not self._pending:
                    return
            self._wake.set()
            await asyncio.sleep(0.05)

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
            event_id=deterministic_event_id(
                request_id=request_id, outcome=outcome, backend=backend
            ),
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
            enqueued_at=time.monotonic(),
        )

    def _annotate_span(self, event: UsageEvent) -> None:
        span = trace.get_current_span()
        if span is None or not span.is_recording():
            USAGE_EVENTS.labels(outcome=event.outcome, sink="otel_skip").inc()
            return
        for key, value in event.as_dict().items():
            if value is None:
                continue
            span.set_attribute(f"ai.runtime.usage.{key}", value)
        USAGE_EVENTS.labels(outcome=event.outcome, sink="otel").inc()

    async def emit(self, event: UsageEvent) -> None:
        self._annotate_span(event)
        webhook = usage_webhook_url()
        if not webhook:
            return
        if not self._accepting:
            USAGE_EVENTS_DROPPED.labels(reason="not_accepting").inc()
            return
        async with self._lock:
            if len(self._pending) >= self._max:
                self._pending.popleft()
                USAGE_EVENTS_DROPPED.labels(reason="buffer_overflow").inc()
            event.enqueued_at = time.monotonic()
            event.attempts = 0
            self._pending.append(_Pending(event=event))
            USAGE_BUFFER_DEPTH.set(len(self._pending))
        self._wake.set()

    async def _run_worker(self) -> None:
        while True:
            webhook = usage_webhook_url()
            if not webhook:
                await asyncio.sleep(0.5)
                continue
            item = await self._next_ready()
            if item is None:
                self._wake.clear()
                with suppress(TimeoutError):
                    await asyncio.wait_for(self._wake.wait(), timeout=1.0)
                continue
            await self._deliver(webhook, item)

    async def _next_ready(self) -> _Pending | None:
        now = time.monotonic()
        async with self._lock:
            for index, item in enumerate(self._pending):
                if item.next_attempt_at <= now:
                    del self._pending[index]
                    USAGE_BUFFER_DEPTH.set(len(self._pending))
                    return item
        return None

    async def _deliver(self, webhook: str, item: _Pending) -> None:
        if self._client is None:
            return
        event = item.event
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
            if event.enqueued_at:
                USAGE_DELIVERY_LAG.observe(max(0.0, time.monotonic() - event.enqueued_at))
        except Exception:  # noqa: BLE001 - retry with backoff
            USAGE_EVENTS.labels(outcome=event.outcome, sink="webhook_error").inc()
            event.attempts += 1
            if event.attempts >= self._max_attempts:
                USAGE_EVENTS_DROPPED.labels(reason="max_attempts").inc()
                return
            delay = min(30.0, 0.25 * (2 ** min(event.attempts, 6)))
            async with self._lock:
                if len(self._pending) >= self._max:
                    self._pending.popleft()
                    USAGE_EVENTS_DROPPED.labels(reason="buffer_overflow").inc()
                self._pending.append(
                    _Pending(event=event, next_attempt_at=time.monotonic() + delay)
                )
                USAGE_BUFFER_DEPTH.set(len(self._pending))


def tokens_from_usage(usage: dict[str, Any] | None) -> tuple[int, int]:
    if not isinstance(usage, dict):
        return 0, 0
    return int(usage.get("prompt_tokens") or 0), int(usage.get("completion_tokens") or 0)


def estimate_gpu_seconds(duration_ms: float) -> float:
    return round(max(0.0, duration_ms) / 1000.0, 4)


def extract_usage_from_sse(chunk: bytes) -> dict[str, Any] | None:
    """Parse OpenAI SSE chunks for a final usage object (stream_options.include_usage)."""
    text = chunk.decode("utf-8", errors="ignore")
    for line in text.splitlines():
        if not line.startswith("data:"):
            continue
        data = line[5:].strip()
        if not data or data == "[DONE]":
            continue
        try:
            payload = json.loads(data)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict) and isinstance(payload.get("usage"), dict):
            return payload["usage"]
    return None
