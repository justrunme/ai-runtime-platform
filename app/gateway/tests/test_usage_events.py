"""Usage event construction and emission."""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from app.gateway.usage_events import UsageEventEmitter, tokens_from_usage


def test_tokens_from_usage() -> None:
    assert tokens_from_usage({"prompt_tokens": 12, "completion_tokens": 3}) == (12, 3)
    assert tokens_from_usage(None) == (0, 0)


@pytest.mark.anyio
async def test_usage_event_webhook_idempotency_header(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(204, request=request)

    monkeypatch.setenv("USAGE_EVENTS_WEBHOOK_URL", "http://collector.test/usage")
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        emitter = UsageEventEmitter(client)
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
        for _ in range(50):
            if seen:
                break
            await asyncio.sleep(0.01)
    assert seen
    assert seen[0].headers.get("idempotency-key") == event.event_id
    body = json.loads(seen[0].content)
    assert body["tenant_id"] == "finance"
    assert body["decision_id"] == "dec-1"
