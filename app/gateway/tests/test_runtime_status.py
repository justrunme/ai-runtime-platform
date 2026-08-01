"""Runtime status and verification API."""

from __future__ import annotations

import httpx
import pytest
from httpx import ASGITransport, AsyncClient

from app.gateway.config import GatewaySettings, ModelTarget
from app.gateway.decisions import InMemoryDecisionStore
from app.gateway.main import app
from app.gateway.runtime_config import RuntimeConfigState
from app.gateway.stores.health import BackendHealthStore


def _settings() -> GatewaySettings:
    return GatewaySettings(
        model_targets={
            "qwen": ModelTarget(
                url="http://primary/v1",
                input_cost_per_million=0,
                output_cost_per_million=0,
            )
        }
    )


@pytest.fixture
async def runtime_client():
    settings = _settings()
    client = httpx.AsyncClient()
    app.state.settings = settings
    app.state.runtime_config = RuntimeConfigState.from_settings(settings)
    app.state.client = client
    app.state.backend_health = BackendHealthStore(settings, client)
    app.state.decision_store = InMemoryDecisionStore()
    app.state.governance = None
    app.state.tenant_attribution = None
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as api:
        yield api
    await client.aclose()


@pytest.mark.anyio
async def test_runtime_status_reports_config_digest(runtime_client: AsyncClient) -> None:
    response = await runtime_client.get("/v1/runtime/status")
    assert response.status_code == 200
    body = response.json()
    assert body["configuration"]["observed_digest"].startswith("sha256:")
    assert body["configuration"]["generation"] == 1
    assert body["routes"]["models"] == ["qwen"]
    assert "healthy" in body["backends"]
    assert body["last_known_good"]["observed_digest"] == body["configuration"]["observed_digest"]


@pytest.mark.anyio
async def test_runtime_verify_detects_model_drift(runtime_client: AsyncClient) -> None:
    status = (await runtime_client.get("/v1/runtime/status")).json()
    ok = await runtime_client.post(
        "/v1/runtime/verify",
        json={"expected": {"config_digest": status["configuration"]["observed_digest"]}},
    )
    assert ok.status_code == 200
    assert ok.json()["verified"] is True

    drift = await runtime_client.post(
        "/v1/runtime/verify",
        json={"expected": {"models": ["qwen", "unknown-model"]}},
    )
    assert drift.status_code == 200
    body = drift.json()
    assert body["verified"] is False
    assert body["differences"][0]["field"] == "models"


@pytest.mark.anyio
async def test_runtime_status_tracks_policy_observation(runtime_client: AsyncClient) -> None:
    app.state.runtime_config.policy.observe(
        bundle_id="production-2026-08",
        digest="sha256:pol",
    )
    response = await runtime_client.get("/v1/runtime/status")
    assert response.json()["policy"]["last_seen_bundle_id"] == "production-2026-08"
    assert response.json()["policy"]["last_seen_digest"] == "sha256:pol"
