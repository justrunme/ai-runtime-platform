import pytest

from app.gateway.tenant import TenantAttributionStore, create_tenant_store


@pytest.mark.anyio
async def test_tenant_store_records_requests_and_tokens() -> None:
    store = TenantAttributionStore()
    await store.record_request("finance", input_tokens=100, output_tokens=50)
    rpm, tokens = await store.usage_snapshot("finance")
    assert rpm == 1
    assert tokens == 150


def test_resolve_team_prefers_header() -> None:
    store = TenantAttributionStore()
    request = type(
        "Request",
        (),
        {"headers": {"x-ai-team": "search", "x-ai-tenant": "ignored"}},
    )()
    assert store.resolve_team(request) == "search"


@pytest.mark.anyio
async def test_redis_tenant_store_is_shared_across_replicas() -> None:
    import fakeredis.aioredis

    from app.gateway.tenant import RedisTenantAttributionStore

    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    replica_a = RedisTenantAttributionStore(redis)
    replica_b = RedisTenantAttributionStore(redis)

    await replica_a.record_request("finance", input_tokens=40, output_tokens=10)
    await replica_b.record_request("finance", input_tokens=20, output_tokens=5)

    rpm, tokens = await replica_a.usage_snapshot("finance")
    assert rpm == 2
    assert tokens == 75
    await redis.aclose()


def test_create_tenant_store_selects_backend_by_redis_url() -> None:
    assert create_tenant_store(None) is None

    import os

    previous = os.environ.get("TENANT_ATTRIBUTION_ENABLED")
    os.environ["TENANT_ATTRIBUTION_ENABLED"] = "true"
    try:
        memory_store = create_tenant_store(None)
        assert isinstance(memory_store, TenantAttributionStore)

        from app.gateway.tenant import RedisTenantAttributionStore

        redis_store = create_tenant_store("redis://localhost:6379/0")
        assert isinstance(redis_store, RedisTenantAttributionStore)
    finally:
        if previous is None:
            os.environ.pop("TENANT_ATTRIBUTION_ENABLED", None)
        else:
            os.environ["TENANT_ATTRIBUTION_ENABLED"] = previous
