"""Usage event construction, retry delivery, and SSE usage parsing."""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from app.gateway.usage_events import (
    UsageEventEmitter,
    deterministic_event_id,
    extract_usage_from_sse,
    tokens_from_usage,
)


def test_tokens_from_usage() -> None:
    assert tokens_from_usage({"prompt_tokens": 12, "completion_tokens": 3}) == (12, 3)
    assert tokens_from_usage(None) == (0, 0)


def test_deterministic_event_id_is_stable() -> None:
    assert deterministic_event_id(
        request_id="req-1", outcome="success", backend="qwen"
    ) == deterministic_event_id(request_id="req-1", outcome="success", backend="qwen")


def test_extract_usage_from_sse() -> None:
    chunk = (
        b'data: {"id":"1","choices":[{"delta":{}}]}\n\n'
        b'data: {"id":"1","choices":[],"usage":{"prompt_tokens":9,"completion_tokens":4}}\n\n'
        b"data: [DONE]\n\n"
    )
    usage = extract_usage_from_sse(chunk)
    assert usage == {"prompt_tokens": 9, "completion_tokens": 4}


@pytest.mark.anyio
async def test_usage_event_webhook_retry_then_success(monkeypatch: pytest.MonkeyPatch) -> None:
    attempts = {"n": 0}
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        attempts["n"] += 1
        seen.append(request)
        if attempts["n"] == 1:
            return httpx.Response(500, request=request)
        return httpx.Response(204, request=request)

    monkeypatch.setenv("USAGE_EVENTS_WEBHOOK_URL", "http://collector.test/usage")
    monkeypatch.setenv("USAGE_EVENTS_MAX_ATTEMPTS", "5")
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        emitter = UsageEventEmitter(client)
        emitter.start()
        event = emitter.build(
            tenant_id="finance",
            request_id="req-1",
            decision_id="dec-1",
            model="qwen",
            backend="qwen",
            input_tokens=10,
            output_tokens=5,
            estimated_cost_usd=0.001,
            duration_ms=120.0,
            outcome="success",
            gpu_seconds=0.12,
        )
        await emitter.emit(event)
        for _ in range(100):
            if attempts["n"] >= 2:
                break
            await asyncio.sleep(0.05)
        await emitter.aclose(drain_timeout_seconds=2.0)
    assert attempts["n"] >= 2
    assert seen[0].headers.get("idempotency-key") == event.event_id
    body = json.loads(seen[-1].content)
    assert body["tenant_id"] == "finance"
    assert body["event_id"] == event.event_id
