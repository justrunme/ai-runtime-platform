"""Pre-reserve + reconcile token/spend accounting through chat completions."""

from __future__ import annotations

import json
import os

import httpx
import pytest

from app.gateway.main import (
    BackendHealthStore,
    GatewaySettings,
    ModelTarget,
    app,
    create_decision_store,
)
from app.gateway.resilience import CircuitBreaker, DrainState, GlobalAdmissionController
from app.gateway.runtime_config import RuntimeConfigState
from app.gateway.tenant import TenantAttributionStore


@pytest.mark.anyio
async def test_chat_reconciles_actual_usage_into_quota(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TENANT_ATTRIBUTION_ENABLED", "true")
    store = TenantAttributionStore()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "id": "chatcmpl-1",
                "choices": [{"message": {"role": "assistant", "content": "ok"}}],
                "usage": {"prompt_tokens": 100, "completion_tokens": 9900, "total_tokens": 10000},
            },
            request=request,
        )

    settings = GatewaySettings(
        model_targets={
            "qwen": ModelTarget(
                url="http://primary/v1",
                input_cost_per_million=1.0,
                output_cost_per_million=2.0,
            )
        }
    )
    upstream = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    app.state.settings = settings
    app.state.runtime_config = RuntimeConfigState.from_settings(settings)
    app.state.client = upstream
    app.state.backend_health = BackendHealthStore(settings, upstream)
    app.state.decision_store = create_decision_store(None)
    app.state.governance = None
    app.state.tenant_attribution = store
    app.state.decision_replay = None
    app.state.circuit_breaker = CircuitBreaker()
    app.state.global_admission = GlobalAdmissionController(max_inflight=64, max_queued=64)
    app.state.drain = DrainState()
    app.state.admission = None

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://gw") as client:
        response = await client.post(
            "/v1/chat/completions",
            json={
                "model": "qwen",
                "messages": [{"role": "user", "content": "hi"}],
                "max_tokens": 16,
            },
            headers={"x-ai-team": "finance"},
        )
    assert response.status_code == 200
    # Without JWT verification, attribution defaults to the platform tenant.
    _, tokens = await store.usage_snapshot("platform")
    # Actual usage (100+9900) replaces the small chars//4 + max_tokens estimate.
    assert tokens == 10000
    spend = await store.spend_snapshot("platform")
    # 100*1 + 9900*2 per million = 0.0199
    assert spend == pytest.approx(0.0199)
    await upstream.aclose()


@pytest.mark.anyio
async def test_governance_sees_reconciled_spend_on_next_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TENANT_ATTRIBUTION_ENABLED", "true")
    store = TenantAttributionStore()
    seen_spend: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/governance/evaluate"):
            body = json.loads(request.content.decode())
            seen_spend.append(float(body["month_to_date_cost_usd"]))
            if body["month_to_date_cost_usd"] > 1.0:
                return httpx.Response(
                    200,
                    json={
                        "final_verdict": "block",
                        "reasons": ["budget"],
                        "stages": {"budget": "block"},
                    },
                    request=request,
                )
            return httpx.Response(
                200,
                json={"final_verdict": "allow", "reasons": ["ok"], "stages": {}},
                request=request,
            )
        return httpx.Response(
            200,
            json={
                "id": "chatcmpl-1",
                "choices": [{"message": {"role": "assistant", "content": "ok"}}],
                "usage": {"prompt_tokens": 500_000, "completion_tokens": 500_000},
            },
            request=request,
        )

    settings = GatewaySettings(
        model_targets={
            "qwen": ModelTarget(
                url="http://primary/v1",
                input_cost_per_million=2.0,
                output_cost_per_million=2.0,
            )
        }
    )
    from app.gateway.governance import GovernanceConfig

    upstream = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    app.state.settings = settings
    app.state.runtime_config = RuntimeConfigState.from_settings(settings)
    app.state.client = upstream
    app.state.backend_health = BackendHealthStore(settings, upstream)
    app.state.decision_store = create_decision_store(None)
    app.state.governance = GovernanceConfig(
        control_plane_url="http://cp",
        enabled=True,
        fail_open=False,
        timeout_seconds=2.0,
        default_team="platform",
        default_owner="gateway",
        default_environment="development",
        default_namespace="ai-dev",
        default_provider="vllm",
        default_action="invoke_model",
        default_cost_per_hour_usd=0.18,
        default_month_to_date_cost_usd=0.0,
        default_forecast_monthly_cost_usd=400.0,
    )
    app.state.tenant_attribution = store
    app.state.decision_replay = None
    app.state.circuit_breaker = CircuitBreaker()
    app.state.global_admission = GlobalAdmissionController(max_inflight=64, max_queued=64)
    app.state.drain = DrainState()
    app.state.admission = None

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://gw") as client:
        first = await client.post(
            "/v1/chat/completions",
            json={
                "model": "qwen",
                "messages": [{"role": "user", "content": "hi"}],
                "max_tokens": 8,
            },
        )
        assert first.status_code == 200
        second = await client.post(
            "/v1/chat/completions",
            json={
                "model": "qwen",
                "messages": [{"role": "user", "content": "hi again"}],
                "max_tokens": 8,
            },
        )
    assert second.status_code == 403
    assert second.json()["error"]["code"] == "governance_blocked"
    assert len(seen_spend) == 2
    assert seen_spend[1] > 1.0
    await upstream.aclose()
    os.environ.pop("TENANT_ATTRIBUTION_ENABLED", None)
