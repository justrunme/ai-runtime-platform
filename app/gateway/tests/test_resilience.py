"""Global admission, circuit breaker, and drain behavior."""

from __future__ import annotations

import httpx
import pytest
from fastapi import HTTPException
from httpx import ASGITransport, AsyncClient

from app.gateway.config import GatewaySettings, ModelTarget
from app.gateway.decisions import InMemoryDecisionStore
from app.gateway.main import app
from app.gateway.resilience import CircuitBreaker, DrainState, GlobalAdmissionController
from app.gateway.runtime_config import RuntimeConfigState
from app.gateway.stores.health import BackendHealthStore


@pytest.mark.anyio
async def test_global_admission_load_shed() -> None:
    controller = GlobalAdmissionController(max_inflight=1, max_queued=0)
    await controller.acquire()
    with pytest.raises(HTTPException) as error:
        await controller.acquire()
    assert error.value.status_code == 503
    assert error.value.detail["error"]["code"] == "gateway_load_shed"
    await controller.release()


def test_circuit_breaker_opens_after_threshold() -> None:
    breaker = CircuitBreaker(failure_threshold=3, recovery_seconds=60)
    assert breaker.allow("qwen")
    breaker.record_failure("qwen")
    breaker.record_failure("qwen")
    assert breaker.allow("qwen")
    breaker.record_failure("qwen")
    assert breaker.allow("qwen") is False
    breaker.record_success("qwen")
    assert breaker.allow("qwen")


@pytest.mark.anyio
async def test_readyz_false_when_draining() -> None:
    settings = GatewaySettings(
        model_targets={
            "qwen": ModelTarget(
                url="http://primary/v1",
                input_cost_per_million=0,
                output_cost_per_million=0,
            )
        }
    )
    client = httpx.AsyncClient()
    app.state.settings = settings
    app.state.runtime_config = RuntimeConfigState.from_settings(settings)
    app.state.client = client
    app.state.backend_health = BackendHealthStore(settings, client)
    app.state.decision_store = InMemoryDecisionStore()
    app.state.governance = None
    app.state.drain = DrainState()
    app.state.drain.begin()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as api:
        response = await api.get("/readyz")
        assert response.status_code == 503
        assert response.json()["status"] == "draining"
        denied = await api.post(
            "/v1/chat/completions",
            json={"model": "qwen", "messages": [{"role": "user", "content": "hi"}]},
        )
        assert denied.status_code == 503
        assert denied.json()["error"]["code"] == "gateway_draining"
    app.state.drain = DrainState()
    await client.aclose()
