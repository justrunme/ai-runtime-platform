"""HA shared-state and readiness contract tests."""

from __future__ import annotations

import httpx
import pytest
from pydantic import ValidationError

from app.gateway.decisions import DecisionRecord, RedisDecisionStore
from app.gateway.main import BackendHealthStore, GatewaySettings, ModelTarget, app
from app.gateway.readiness import build_readiness_report


def test_multi_replica_requires_redis() -> None:
    with pytest.raises(ValidationError):
        GatewaySettings.model_validate(
            {
                "model_targets": {
                    "qwen": {
                        "url": "http://model",
                        "input_cost_per_million": 0,
                        "output_cost_per_million": 0,
                    }
                },
                "gateway_replicas": 2,
                "require_shared_state": True,
                "redis_url": None,
            }
        )


def test_single_node_allows_in_memory() -> None:
    settings = GatewaySettings.model_validate(
        {
            "model_targets": {
                "qwen": {
                    "url": "http://model",
                    "input_cost_per_million": 0,
                    "output_cost_per_million": 0,
                }
            },
            "gateway_replicas": 1,
            "require_shared_state": False,
            "redis_url": None,
        }
    )
    assert settings.redis_url is None


@pytest.mark.anyio
async def test_redis_decision_store_atomic_shadow_patch() -> None:
    import fakeredis.aioredis

    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    store = RedisDecisionStore(redis)
    await store.put(
        DecisionRecord(
            request_id="req-1",
            requested_model="qwen",
            selected_backend="qwen",
            routing_reason="direct",
            fallback_used=False,
            health_score=90,
            duration_ms=12.0,
            shadow_backend="canary",
        )
    )
    await store.patch_shadow("req-1", outcome="success", duration_ms=33.5)
    record = await store.get("req-1")
    assert record is not None
    assert record.shadow_outcome == "success"
    assert record.shadow_duration_ms == 33.5
    assert await store.ping() is True
    await store.aclose()


@pytest.mark.anyio
async def test_readyz_reports_route_registry() -> None:
    settings = GatewaySettings(
        model_targets={
            "qwen": ModelTarget(
                url="http://primary/v1", input_cost_per_million=0, output_cost_per_million=0
            )
        }
    )
    client = httpx.AsyncClient()
    app.state.settings = settings
    app.state.client = client
    app.state.backend_health = BackendHealthStore(settings, client)
    from app.gateway.decisions import create_decision_store

    app.state.decision_store = create_decision_store(None)
    app.state.governance = None

    ready, report = await build_readiness_report(app)
    assert ready is True
    assert report["checks"]["route_registry"]["ok"] is True
    assert report["checks"]["redis"]["ok"] is True
    await client.aclose()


@pytest.mark.anyio
async def test_ewma_health_recovers_after_failures() -> None:
    settings = GatewaySettings(
        model_targets={
            "qwen": ModelTarget(
                url="http://primary/v1", input_cost_per_million=0, output_cost_per_million=0
            )
        }
    )
    store = BackendHealthStore(settings, httpx.AsyncClient())
    for _ in range(5):
        await store.record_request("qwen", success=False)
    low = (await store.routing_signal("qwen"))[0]
    for _ in range(20):
        await store.record_request("qwen", success=True)
    high = (await store.routing_signal("qwen"))[0]
    assert low < 50
    assert high > low
    await store.aclose()
