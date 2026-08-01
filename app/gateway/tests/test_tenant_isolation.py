"""Tenant-scoped decisions, allowlists, and admission limits."""

from __future__ import annotations

import json

import httpx
import pytest
from fastapi import HTTPException
from httpx import ASGITransport, AsyncClient

from app.gateway.admission import TenantAdmissionController
from app.gateway.config import GatewaySettings, ModelTarget
from app.gateway.decisions import DecisionRecord, InMemoryDecisionStore
from app.gateway.main import app
from app.gateway.runtime_config import RuntimeConfigState
from app.gateway.stores.health import BackendHealthStore
from app.gateway.tenant_policy import (
    TenantPolicyBundle,
    TenantRuntimePolicy,
    reset_tenant_policy_cache,
)


def _settings() -> GatewaySettings:
    return GatewaySettings(
        model_targets={
            "qwen": ModelTarget(
                url="http://primary/v1",
                input_cost_per_million=0,
                output_cost_per_million=0,
            ),
            "llama": ModelTarget(
                url="http://secondary/v1",
                input_cost_per_million=0,
                output_cost_per_million=0,
            ),
        }
    )


@pytest.fixture
def tenant_policy(monkeypatch: pytest.MonkeyPatch) -> None:
    reset_tenant_policy_cache()
    monkeypatch.setenv(
        "TENANT_RUNTIME_POLICY",
        json.dumps(
            {
                "tenants": {
                    "finance": {
                        "allowedModels": ["qwen"],
                        "maxConcurrentRequests": 1,
                        "maxQueuedRequests": 0,
                    },
                    "research": {
                        "allowedModels": ["qwen", "llama"],
                        "maxConcurrentRequests": 5,
                        "maxQueuedRequests": 10,
                    },
                },
                "auditorGroups": ["ai-auditors"],
            }
        ),
    )
    reset_tenant_policy_cache()
    yield
    reset_tenant_policy_cache()


@pytest.mark.anyio
async def test_decision_lookup_is_tenant_scoped(tenant_policy) -> None:
    store = InMemoryDecisionStore()
    await store.put(
        DecisionRecord(
            request_id="req-t",
            requested_model="qwen",
            selected_backend="qwen",
            routing_reason="primary",
            fallback_used=False,
            health_score=100,
            duration_ms=1.0,
            tenant_id="finance",
        )
    )
    assert await store.get("req-t", tenant_id="finance") is not None
    assert await store.get("req-t", tenant_id="research") is None
    assert await store.get("req-t", any_tenant=True) is not None


@pytest.mark.anyio
async def test_tenant_model_allowlist_and_decision_acl(
    tenant_policy, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _settings()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "id": "chatcmpl-1",
                "object": "chat.completion",
                "choices": [{"message": {"role": "assistant", "content": "ok"}}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1},
            },
            request=request,
        )

    upstream = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    app.state.settings = settings
    app.state.runtime_config = RuntimeConfigState.from_settings(settings)
    app.state.admission = TenantAdmissionController()
    app.state.client = upstream
    app.state.backend_health = BackendHealthStore(settings, upstream)
    app.state.decision_store = InMemoryDecisionStore()
    app.state.governance = None
    app.state.tenant_attribution = None

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # Spoof headers must not grant finance tenant without JWT claims.
        monkeypatch.setattr(
            "app.gateway.tenant_context._claims",
            lambda request: {"tenant": "finance", "sub": "alice"},
        )
        denied = await client.post(
            "/v1/chat/completions",
            json={"model": "llama", "messages": [{"role": "user", "content": "hi"}]},
            headers={"x-request-id": "fin-1"},
        )
        assert denied.status_code == 403
        assert denied.json()["error"]["code"] == "tenant_model_denied"

        allowed = await client.post(
            "/v1/chat/completions",
            json={"model": "qwen", "messages": [{"role": "user", "content": "hi"}]},
            headers={"x-request-id": "fin-2"},
        )
        assert allowed.status_code == 200
        decision = await client.get("/v1/decisions/fin-2")
        assert decision.status_code == 200
        assert decision.json()["tenant_id"] == "finance"

        monkeypatch.setattr(
            "app.gateway.tenant_context._claims",
            lambda request: {"tenant": "research", "sub": "bob"},
        )
        cross = await client.get("/v1/decisions/fin-2")
        assert cross.status_code == 404

        monkeypatch.setattr(
            "app.gateway.tenant_context._claims",
            lambda request: {"tenant": "research", "groups": ["ai-auditors"], "sub": "audit"},
        )
        audited = await client.get("/v1/decisions/fin-2")
        assert audited.status_code == 200

    await upstream.aclose()


@pytest.mark.anyio
async def test_tenant_concurrency_limit_returns_429(tenant_policy) -> None:
    bundle = TenantPolicyBundle(
        tenants={
            "finance": TenantRuntimePolicy(
                allowedModels=["qwen"],
                maxConcurrentRequests=1,
                maxQueuedRequests=0,
            )
        }
    )
    controller = TenantAdmissionController(bundle)
    first = await controller.acquire_lease("finance")
    with pytest.raises(HTTPException) as error:
        await controller.acquire_lease("finance")
    assert error.value.status_code == 429
    assert error.value.detail["error"]["code"] == "tenant_concurrency_exceeded"
    assert error.value.headers["Retry-After"] == "1"
    await first.release()
