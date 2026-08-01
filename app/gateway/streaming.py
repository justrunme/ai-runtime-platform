"""Observed SSE/stream wrappers for honest completion accounting."""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any

import httpx
from prometheus_client import Counter, Histogram

STREAM_OUTCOMES = Counter(
    "gateway_stream_outcomes_total",
    "Terminal outcomes for OpenAI-compatible streaming responses.",
    ["outcome", "selected_backend"],
)
STREAM_TTFT = Histogram(
    "gateway_stream_ttft_seconds",
    "Time to first streamed byte from the selected backend.",
    ["selected_backend"],
    buckets=(0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2, 5, 10),
)
STREAM_DURATION = Histogram(
    "gateway_stream_duration_seconds",
    "Full stream duration until success, interrupt, or disconnect.",
    ["selected_backend", "outcome"],
    buckets=(0.05, 0.1, 0.25, 0.5, 1, 2, 5, 10, 30, 60, 120),
)


@dataclass(frozen=True)
class StreamObservation:
    outcome: str
    duration_ms: float
    ttft_ms: float | None
    bytes_sent: int
    saw_done: bool
    usage: dict[str, Any] | None = None


OnComplete = Callable[[StreamObservation], Awaitable[None]]


def classify_stream_outcome(
    *,
    bytes_sent: int,
    saw_done: bool,
    error: BaseException | None,
) -> str:
    """Classify terminal stream state for honest telemetry."""
    if error is None:
        if saw_done:
            return "success"
        if bytes_sent == 0:
            return "empty_stream"
        # Some OpenAI-compatible backends omit [DONE]; non-empty EOF is still success.
        return "success"

    if isinstance(error, (asyncio.CancelledError, GeneratorExit)):
        return "client_disconnected"
    if isinstance(error, httpx.HTTPError):
        if bytes_sent == 0:
            return "upstream_error_before_first_byte"
        return "upstream_interrupted"
    if bytes_sent == 0:
        return "upstream_error_before_first_byte"
    return "upstream_interrupted"


async def observe_upstream_stream(
    upstream: httpx.Response,
    *,
    selected_backend: str,
    on_complete: OnComplete | None = None,
) -> AsyncIterator[bytes]:
    """Yield upstream bytes and finalize metrics/state after the stream ends."""
    from app.gateway.usage_events import extract_usage_from_sse

    started_at = time.monotonic()
    first_byte_at: float | None = None
    bytes_sent = 0
    saw_done = False
    usage: dict[str, Any] | None = None
    error: BaseException | None = None
    try:
        async for chunk in upstream.aiter_bytes():
            if first_byte_at is None:
                first_byte_at = time.monotonic()
                STREAM_TTFT.labels(selected_backend=selected_backend).observe(
                    first_byte_at - started_at
                )
            bytes_sent += len(chunk)
            if b"[DONE]" in chunk:
                saw_done = True
            found = extract_usage_from_sse(chunk)
            if found is not None:
                usage = found
            yield chunk
    except BaseException as exc:  # noqa: BLE001 - must classify every terminal path
        error = exc
        raise
    finally:
        outcome = classify_stream_outcome(bytes_sent=bytes_sent, saw_done=saw_done, error=error)
        duration_s = max(0.0, time.monotonic() - started_at)
        observation = StreamObservation(
            outcome=outcome,
            duration_ms=round(duration_s * 1000, 2),
            ttft_ms=None
            if first_byte_at is None
            else round((first_byte_at - started_at) * 1000, 2),
            bytes_sent=bytes_sent,
            saw_done=saw_done,
            usage=usage,
        )
        STREAM_OUTCOMES.labels(outcome=outcome, selected_backend=selected_backend).inc()
        STREAM_DURATION.labels(selected_backend=selected_backend, outcome=outcome).observe(
            duration_s
        )
        try:
            if on_complete is not None:
                await on_complete(observation)
        finally:
            await upstream.aclose()


def stream_headers(
    base: Mapping[str, str],
    *,
    selected_backend: str,
    fallback_used: bool,
) -> dict[str, str]:
    headers = dict(base)
    headers["x-selected-backend"] = selected_backend
    if fallback_used:
        headers["x-fallback-used"] = "true"
    headers["x-ai-stream-observed"] = "true"
    return headers


def observation_as_dict(observation: StreamObservation) -> dict[str, Any]:
    return {
        "stream_outcome": observation.outcome,
        "stream_duration_ms": observation.duration_ms,
        "stream_ttft_ms": observation.ttft_ms,
        "stream_bytes_sent": observation.bytes_sent,
        "stream_saw_done": observation.saw_done,
    }
