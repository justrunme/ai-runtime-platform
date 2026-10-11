import asyncio
import time

import pytest

from app.gateway.tenant import WINDOW_SECONDS, TenantAttributionStore, create_tenant_store


@pytest.mark.anyio
async def test_tenant_store_records_requests_and_tokens() -> None:
    store = TenantAttributionStore()
    reservation = await store.reserve_request("finance", input_tokens=100, output_tokens=50)
    rpm, tokens = await store.usage_snapshot("finance")
    assert rpm == 1
    assert tokens == 150
    assert reservation.tokens_today == 150


def test_resolve_team_uses_jwt_claims_not_headers() -> None:
    store = TenantAttributionStore()
    # Spoof headers must not select tenant once JWT claims are the source of truth.
    request = type(
        "Request",
        (),
        {
            "headers": {"x-ai-team": "search", "x-ai-tenant": "ignored"},
            "state": type("State", (), {"identity_claims": {"tenant": "finance"}})(),
        },
    )()
    assert store.resolve_team(request) == "finance"

    anonymous = type("Request", (), {"headers": {"x-ai-team": "search"}})()
    assert store.resolve_team(anonymous) == "platform"


@pytest.mark.anyio
async def test_redis_tenant_store_is_shared_across_replicas() -> None:
    import fakeredis.aioredis

    from app.gateway.tenant import RedisTenantAttributionStore

    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    replica_a = RedisTenantAttributionStore(redis)
    replica_b = RedisTenantAttributionStore(redis)

    await replica_a.reserve_request("finance", input_tokens=40, output_tokens=10)
    await replica_b.reserve_request("finance", input_tokens=20, output_tokens=5)

    rpm, tokens = await replica_a.usage_snapshot("finance")
    assert rpm == 2
    assert tokens == 75
    await redis.aclose()


@pytest.mark.anyio
async def test_redis_concurrent_reserves_lose_no_updates() -> None:
    import fakeredis.aioredis

    from app.gateway.tenant import RedisTenantAttributionStore

    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    store = RedisTenantAttributionStore(redis)
    concurrency = 200

    async def _one(i: int) -> None:
        await store.reserve_request(
            "finance",
            input_tokens=1,
            output_tokens=0,
            member_id=f"req-{i}",
        )

    await asyncio.gather(*(_one(i) for i in range(concurrency)))
    rpm, tokens = await store.usage_snapshot("finance")
    assert rpm == concurrency
    assert tokens == concurrency
    await redis.aclose()


@pytest.mark.anyio
async def test_redis_sliding_window_evicts_old_requests() -> None:
    import fakeredis.aioredis

    from app.gateway.tenant import RedisTenantAttributionStore

    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    store = RedisTenantAttributionStore(redis)
    t0 = 1_700_000_000.0
    await store.reserve_request("finance", input_tokens=1, output_tokens=0, member_id="old", now=t0)
    await store.reserve_request(
        "finance",
        input_tokens=1,
        output_tokens=0,
        member_id="new",
        now=t0 + WINDOW_SECONDS + 1,
    )
    rpm, tokens = await store.usage_snapshot("finance", now=t0 + WINDOW_SECONDS + 1)
    assert rpm == 1
    assert tokens == 2
    await redis.aclose()


@pytest.mark.anyio
async def test_memory_sliding_window_evicts_old_requests() -> None:
    store = TenantAttributionStore()
    t0 = time.monotonic()
    await store.reserve_request("finance", input_tokens=1, output_tokens=0, member_id="old", now=t0)
    await store.reserve_request(
        "finance",
        input_tokens=1,
        output_tokens=0,
        member_id="new",
        now=t0 + WINDOW_SECONDS + 1,
    )
    rpm, tokens = await store.usage_snapshot("finance", now=t0 + WINDOW_SECONDS + 1)
    assert rpm == 1
    assert tokens == 2


@pytest.mark.anyio
async def test_refund_reservation_restores_quota() -> None:
    import fakeredis.aioredis

    from app.gateway.tenant import RedisTenantAttributionStore

    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    store = RedisTenantAttributionStore(redis)
    reservation = await store.reserve_request(
        "finance", input_tokens=10, output_tokens=5, spend_usd=0.01
    )
    await store.refund_reservation(reservation)
    rpm, tokens = await store.usage_snapshot("finance")
    assert rpm == 0
    assert tokens == 0
    assert await store.spend_snapshot("finance") == 0.0
    await redis.aclose()


@pytest.mark.anyio
async def test_reconcile_usage_adjusts_tokens_and_spend() -> None:
    import fakeredis.aioredis

    from app.gateway.tenant import RedisTenantAttributionStore

    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    store = RedisTenantAttributionStore(redis)
    reservation = await store.reserve_request(
        "finance", input_tokens=10, output_tokens=10, spend_usd=0.002
    )
    tokens, spend = await store.reconcile_usage(
        reservation,
        actual_input_tokens=100,
        actual_output_tokens=9900,
        actual_spend_usd=1.5,
    )
    assert tokens == 10000
    assert spend == pytest.approx(1.5)
    _, snap_tokens = await store.usage_snapshot("finance")
    assert snap_tokens == 10000
    assert await store.spend_snapshot("finance") == pytest.approx(1.5)
    await redis.aclose()


@pytest.mark.anyio
async def test_memory_reconcile_and_spend_feed_budget_signal() -> None:
    store = TenantAttributionStore()
    reservation = await store.reserve_request(
        "finance", input_tokens=5, output_tokens=5, spend_usd=0.001
    )
    await store.reconcile_usage(
        reservation,
        actual_input_tokens=50,
        actual_output_tokens=50,
        actual_spend_usd=2.0,
    )
    assert await store.spend_snapshot("finance") == pytest.approx(2.0)
    # Next reservation sees prior spend in month_to_date.
    next_res = await store.reserve_request(
        "finance", input_tokens=1, output_tokens=0, spend_usd=0.5
    )
    assert next_res.month_to_date_cost_usd == pytest.approx(2.5)


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
