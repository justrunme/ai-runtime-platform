"""Streaming lifecycle observation tests."""

from __future__ import annotations

import httpx
import pytest

from app.gateway.main import (
    BackendHealthStore,
    GatewaySettings,
    ModelTarget,
    app,
    create_decision_store,
)
from app.gateway.streaming import StreamObservation, observe_upstream_stream
from app.gateway.tenant import create_tenant_store


@pytest.mark.anyio
async def test_observe_upstream_stream_marks_success_on_done() -> None:
    chunks: list[bytes] = []

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=b'data: {"id":"1"}\n\ndata: [DONE]\n\n',
            headers={"content-type": "text/event-stream"},
            request=request,
        )

    observations: list[StreamObservation] = []

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        upstream = await client.send(
            client.build_request("POST", "http://model/v1/chat/completions"), stream=True
        )

        async def on_complete(observation: StreamObservation) -> None:
            observations.append(observation)

        async for chunk in observe_upstream_stream(
            upstream, selected_backend="qwen", on_complete=on_complete
        ):
            chunks.append(chunk)

    assert b"[DONE]" in b"".join(chunks)
    assert observations[0].outcome == "success"
    assert observations[0].saw_done is True
    assert observations[0].ttft_ms is not None


@pytest.mark.anyio
async def test_streaming_completion_records_decision_after_body() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=b"data: hello\n\ndata: [DONE]\n\n",
            headers={"content-type": "text/event-stream"},
            request=request,
        )

    settings = GatewaySettings(
        model_targets={
            "qwen": ModelTarget(
                url="http://primary/v1", input_cost_per_million=0, output_cost_per_million=0
            )
        }
    )
    upstream = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    app.state.settings = settings
    app.state.client = upstream
    app.state.backend_health = BackendHealthStore(settings, upstream)
    app.state.decision_store = create_decision_store(None)
    app.state.governance = None
    app.state.tenant_attribution = create_tenant_store(None)

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://gw") as client:
        response = await client.post(
            "/v1/chat/completions",
            json={"model": "qwen", "stream": True, "messages": []},
        )
        assert response.status_code == 200
        assert response.headers["x-ai-stream-observed"] == "true"
        body = response.content
        assert b"[DONE]" in body
        request_id = response.headers["x-request-id"]
        record = await app.state.decision_store.get(request_id)
        assert record is not None
        assert record.stream_outcome == "success"
        assert record.stream_ttft_ms is not None
    await upstream.aclose()
