"""Official OpenAI Python SDK compatibility against the ASGI gateway."""

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
from app.gateway.tenant import create_tenant_store


def _bootstrap(handler) -> httpx.AsyncClient:
    settings = GatewaySettings(
        model_targets={
            "gpt-test": ModelTarget(
                url="http://primary/v1",
                input_cost_per_million=0,
                output_cost_per_million=0,
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
    return upstream


@pytest.mark.anyio
async def test_openai_sdk_chat_completion() -> None:
    openai = pytest.importorskip("openai")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "id": "chatcmpl-test",
                "object": "chat.completion",
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": "hello"},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 3, "completion_tokens": 1, "total_tokens": 4},
            },
            request=request,
        )

    upstream = _bootstrap(handler)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://gw") as http_client:
        client = openai.AsyncOpenAI(
            api_key="test",
            base_url="http://gw/v1",
            http_client=http_client,
        )
        completion = await client.chat.completions.create(
            model="gpt-test",
            messages=[{"role": "user", "content": "hi"}],
        )
        assert completion.choices[0].message.content == "hello"
        assert completion.usage is not None
    await upstream.aclose()


@pytest.mark.anyio
async def test_openai_sdk_unknown_model_is_not_found() -> None:
    openai = pytest.importorskip("openai")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"choices": []}, request=request)

    upstream = _bootstrap(handler)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://gw") as http_client:
        client = openai.AsyncOpenAI(
            api_key="test",
            base_url="http://gw/v1",
            http_client=http_client,
        )
        with pytest.raises(openai.APIStatusError) as error:
            await client.chat.completions.create(
                model="missing-model",
                messages=[{"role": "user", "content": "hi"}],
            )
        assert error.value.status_code == 404
    await upstream.aclose()
