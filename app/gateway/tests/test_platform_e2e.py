"""Combined Control Plane + Runtime golden paths (ASGI / MockTransport).

These tests prove cross-component contracts without Kind. Compose/Kind evidence
lives under deploy/e2e for operator-run HA restart scenarios.
"""

from __future__ import annotations

import httpx
import pytest

from app.gateway.governance import GovernanceConfig
from app.gateway.main import (
    BackendHealthStore,
    GatewaySettings,
    ModelTarget,
    app,
    create_decision_store,
)
from app.gateway.tenant import create_tenant_store


def _governance() -> GovernanceConfig:
    return GovernanceConfig(
        control_plane_url="http://control-plane",
        enabled=True,
        fail_open=False,
        timeout_seconds=2.0,
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


def _bootstrap(handler, *, redis_url: str | None = "redis://fake") -> httpx.AsyncClient:
    settings = GatewaySettings(
        model_targets={
            "qwen": ModelTarget(
                url="http://model/v1",
                input_cost_per_million=0,
                output_cost_per_million=0,
            )
        },
        redis_url=redis_url if redis_url and redis_url != "redis://fake" else None,
        require_shared_state=False,
        gateway_replicas=1,
        profile="local",
    )
    upstream = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    app.state.settings = settings
    app.state.client = upstream
    app.state.backend_health = BackendHealthStore(settings, upstream)
    # Use in-memory decision store; shared Redis HA is covered by decision store tests
    # and deploy/e2e compose. Here we still exercise approval + allow/block contracts.
    app.state.decision_store = create_decision_store(None)
    app.state.governance = _governance()
    app.state.tenant_attribution = create_tenant_store(None)
    return upstream


@pytest.mark.anyio
async def test_allow_reaches_inference_backend() -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        if request.url.path.endswith("/governance/evaluate"):
            return httpx.Response(
                200,
                json={"final_verdict": "allow", "reasons": [], "stages": {}},
                request=request,
            )
        return httpx.Response(
            200,
            json={
                "id": "chatcmpl-1",
                "choices": [{"message": {"role": "assistant", "content": "ok"}}],
                "usage": {},
            },
            request=request,
        )

    upstream = _bootstrap(handler)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://gw") as client:
        response = await client.post(
            "/v1/chat/completions",
            json={"model": "qwen", "messages": [{"role": "user", "content": "hi"}]},
        )
    assert response.status_code == 200
    assert "/governance/evaluate" in calls
    assert any(path.endswith("/chat/completions") for path in calls)
    await upstream.aclose()


@pytest.mark.anyio
async def test_block_does_not_call_model_backend() -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        if request.url.path.endswith("/governance/evaluate"):
            return httpx.Response(
                200,
                json={"final_verdict": "block", "reasons": ["policy"], "stages": {}},
                request=request,
            )
        return httpx.Response(200, json={"usage": {}}, request=request)

    upstream = _bootstrap(handler)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://gw") as client:
        response = await client.post(
            "/v1/chat/completions",
            json={"model": "qwen", "messages": [{"role": "user", "content": "hi"}]},
        )
    assert response.status_code == 403
    assert calls == ["/governance/evaluate"]
    await upstream.aclose()


@pytest.mark.anyio
async def test_approval_required_then_retry_with_header() -> None:
    calls: list[str] = []
    seen_approval: list[str | None] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        if request.url.path.endswith("/governance/evaluate"):
            approval = request.headers.get("x-ai-approval-id")
            seen_approval.append(approval)
            if approval == "apr_ok":
                return httpx.Response(
                    200,
                    json={"final_verdict": "allow", "reasons": [], "stages": {}},
                    request=request,
                )
            return httpx.Response(
                200,
                json={
                    "final_verdict": "approval_required",
                    "approval_id": "apr_ok",
                    "decision_id": "dec_1",
                    "policy_digest": "pol",
                    "request_digest": "req",
                    "reasons": [],
                    "stages": {},
                },
                request=request,
            )
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": "approved"}}], "usage": {}},
            request=request,
        )

    upstream = _bootstrap(handler)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://gw") as client:
        blocked = await client.post(
            "/v1/chat/completions",
            json={"model": "qwen", "messages": [{"role": "user", "content": "hi"}]},
        )
        assert blocked.status_code == 409
        body = blocked.json()
        assert body["error"]["code"] == "governance_approval_required"
        assert body["approval_id"] == "apr_ok"
        assert blocked.headers["x-ai-approval-id"] == "apr_ok"

        allowed = await client.post(
            "/v1/chat/completions",
            json={"model": "qwen", "messages": [{"role": "user", "content": "hi"}]},
            headers={"x-ai-approval-id": "apr_ok"},
        )
        assert allowed.status_code == 200
    assert seen_approval == [None, "apr_ok"]
    assert any(path.endswith("/chat/completions") for path in calls)
    await upstream.aclose()


@pytest.mark.anyio
async def test_control_plane_unavailable_fail_closed() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/governance/evaluate"):
            raise httpx.ConnectError("cp down", request=request)
        return httpx.Response(200, json={"usage": {}}, request=request)

    upstream = _bootstrap(handler)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://gw") as client:
        response = await client.post(
            "/v1/chat/completions",
            json={"model": "qwen", "messages": [{"role": "user", "content": "hi"}]},
        )
    assert response.status_code == 503
    body = response.json()
    assert "detail" not in body
    assert body["error"]["code"] == "control_plane_unavailable"
    await upstream.aclose()
