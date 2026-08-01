"""Streaming lifecycle observation tests."""

from __future__ import annotations

import asyncio

import httpx
import pytest

from app.gateway.main import (
    BackendHealthStore,
    GatewaySettings,
    ModelTarget,
    app,
    create_decision_store,
)
from app.gateway.streaming import (
    StreamObservation,
    classify_stream_outcome,
    observe_upstream_stream,
)
from app.gateway.tenant import create_tenant_store


def test_classify_stream_outcome_matrix() -> None:
    assert classify_stream_outcome(bytes_sent=10, saw_done=True, error=None) == "success"
    assert classify_stream_outcome(bytes_sent=10, saw_done=False, error=None) == "success"
    assert classify_stream_outcome(bytes_sent=0, saw_done=False, error=None) == "empty_stream"
    assert (
        classify_stream_outcome(bytes_sent=0, saw_done=False, error=httpx.ReadError("boom"))
        == "upstream_error_before_first_byte"
    )
    assert (
        classify_stream_outcome(bytes_sent=4, saw_done=False, error=httpx.ReadError("boom"))
        == "upstream_interrupted"
    )
    assert (
        classify_stream_outcome(bytes_sent=4, saw_done=False, error=asyncio.CancelledError())
        == "client_disconnected"
    )


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
async def test_observe_upstream_stream_marks_upstream_interrupted() -> None:
    class BrokenResponse:
        def __init__(self) -> None:
            self._chunks = [b"data: one\n\n", b"data: two\n\n"]

        async def aiter_bytes(self):
            yield self._chunks[0]
            yield self._chunks[1]
            raise httpx.ReadError("upstream dropped")

        async def aclose(self) -> None:
            return None

    observations: list[StreamObservation] = []

    async def on_complete(observation: StreamObservation) -> None:
        observations.append(observation)

    with pytest.raises(httpx.ReadError):
        async for _ in observe_upstream_stream(
            BrokenResponse(),  # type: ignore[arg-type]
            selected_backend="qwen",
            on_complete=on_complete,
        ):
            pass

    assert observations[0].outcome == "upstream_interrupted"
    assert observations[0].bytes_sent > 0


@pytest.mark.anyio
async def test_observe_upstream_stream_marks_client_disconnect() -> None:
    class CancelledResponse:
        async def aiter_bytes(self):
            yield b"data: partial\n\n"
            raise asyncio.CancelledError()

        async def aclose(self) -> None:
            return None

    observations: list[StreamObservation] = []

    async def on_complete(observation: StreamObservation) -> None:
        observations.append(observation)

    with pytest.raises(asyncio.CancelledError):
        async for _ in observe_upstream_stream(
            CancelledResponse(),  # type: ignore[arg-type]
            selected_backend="qwen",
            on_complete=on_complete,
        ):
            pass

    assert observations[0].outcome == "client_disconnected"


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
