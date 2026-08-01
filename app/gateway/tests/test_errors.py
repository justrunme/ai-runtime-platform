"""OpenAI-compatible error envelope contract tests."""

from __future__ import annotations

import httpx
import pytest

from app.gateway.errors import error_body, normalize_error_payload
from app.gateway.governance import GovernanceConfig
from app.gateway.main import (
    BackendHealthStore,
    GatewaySettings,
    ModelTarget,
    app,
    create_decision_store,
)
from app.gateway.tenant import create_tenant_store


def test_error_body_is_top_level_openai_shape() -> None:
    payload = error_body(code="x", message="y", error_type="api_error", request_id="r1")
    assert "detail" not in payload
    assert payload["error"]["code"] == "x"
    assert payload["request_id"] == "r1"


def test_normalize_unwraps_fastapi_detail_wrapper() -> None:
    nested = {"detail": error_body(code="wrapped", message="inner")}
    assert (
        normalize_error_payload(nested, default_code="http_error", default_message="failed")[
            "error"
        ]["code"]
        == "wrapped"
    )


def _bootstrap(handler, *, governance: GovernanceConfig | None = None) -> httpx.AsyncClient:
    settings = GatewaySettings(
        model_targets={
            "qwen": ModelTarget(
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
    app.state.governance = governance
    app.state.tenant_attribution = create_tenant_store(None)
    return upstream


@pytest.mark.anyio
async def test_unknown_model_error_envelope() -> None:
    upstream = _bootstrap(lambda request: httpx.Response(200, json={}, request=request))
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://gw") as client:
        response = await client.post(
            "/v1/chat/completions",
            json={"model": "missing", "messages": []},
            headers={"x-request-id": "req-missing"},
        )
    assert response.status_code == 404
    body = response.json()
    assert "detail" not in body
    assert body["error"]["code"] == "model_not_found"
    assert body["request_id"] == "req-missing"
    await upstream.aclose()


@pytest.mark.anyio
async def test_validation_error_envelope() -> None:
    upstream = _bootstrap(lambda request: httpx.Response(200, json={}, request=request))
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://gw") as client:
        response = await client.post(
            "/v1/chat/completions",
            content=b"{",
            headers={"content-type": "application/json"},
        )
    assert response.status_code == 422
    body = response.json()
    assert "detail" not in body
    assert body["error"]["code"] == "invalid_request"
    await upstream.aclose()


@pytest.mark.anyio
async def test_governance_block_error_envelope() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/governance/evaluate"):
            return httpx.Response(
                200,
                json={
                    "final_verdict": "block",
                    "reasons": ["denied"],
                    "stages": {},
                    "decision_id": "dec_1",
                },
                request=request,
            )
        return httpx.Response(200, json={"usage": {}}, request=request)

    governance = GovernanceConfig(
        control_plane_url="http://cp",
        enabled=True,
        fail_open=False,
        timeout_seconds=1.0,
        default_team="platform",
        default_owner="gateway",
        default_environment="development",
        default_namespace="ai-dev",
        default_provider="ollama",
        default_action="invoke_model",
        default_cost_per_hour_usd=0.18,
        default_month_to_date_cost_usd=100.0,
        default_forecast_monthly_cost_usd=400.0,
    )
    upstream = _bootstrap(handler, governance=governance)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://gw") as client:
        response = await client.post(
            "/v1/chat/completions",
            json={"model": "qwen", "messages": [{"role": "user", "content": "hi"}]},
        )
    assert response.status_code == 403
    body = response.json()
    assert "detail" not in body
    assert body["error"]["code"] == "governance_blocked"
    assert body["final_verdict"] == "block"
    await upstream.aclose()


@pytest.mark.anyio
async def test_upstream_unavailable_error_envelope() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down", request=request)

    upstream = _bootstrap(handler)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://gw") as client:
        response = await client.post(
            "/v1/chat/completions",
            json={"model": "qwen", "messages": []},
        )
    assert response.status_code == 502
    body = response.json()
    assert "detail" not in body
    assert body["error"]["message"] == "model backend unavailable"
    await upstream.aclose()
