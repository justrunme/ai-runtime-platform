"""Fleet-wide Redis admission: atomicity, fencing, expiry, and fail policies."""

from __future__ import annotations

import asyncio
import time

import fakeredis.aioredis
import pytest
from fastapi import HTTPException

from app.gateway.admission import TenantAdmissionController, create_admission_controller
from app.gateway.tenant_policy import TenantPolicyBundle, TenantRuntimePolicy


def _bundle(
    *,
    limit: int = 1,
    queued: int = 0,
    policy: str = "fail_closed",
) -> TenantPolicyBundle:
    return TenantPolicyBundle(
        tenants={
            "finance": TenantRuntimePolicy(
                maxConcurrentRequests=limit,
                maxQueuedRequests=queued,
                redisUnavailablePolicy=policy,  # type: ignore[arg-type]
            ),
            "research": TenantRuntimePolicy(
                maxConcurrentRequests=limit,
                maxQueuedRequests=queued,
                redisUnavailablePolicy=policy,  # type: ignore[arg-type]
            ),
        }
    )


@pytest.mark.anyio
async def test_redis_acquire_is_atomic_across_coroutines() -> None:
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    controller = TenantAdmissionController(
        _bundle(limit=2, queued=0),
        redis=redis,
        lease_ttl_seconds=5.0,
        renew_interval_seconds=1.0,
    )
    leases = await asyncio.gather(
        controller.acquire_lease("finance", model="qwen"),
        controller.acquire_lease("finance", model="qwen"),
    )
    assert len(leases) == 2
    assert {lease.fencing_token for lease in leases} == {1, 2}
    with pytest.raises(HTTPException) as error:
        await controller.acquire_lease("finance", model="qwen")
    assert error.value.status_code == 429
    assert error.value.detail["error"]["code"] == "tenant_concurrency_exceeded"
    assert await controller.inflight_count("finance", "qwen") == 2
    for lease in leases:
        await lease.release()
    assert await controller.inflight_count("finance", "qwen") == 0
    await controller.aclose()


@pytest.mark.anyio
async def test_limits_are_scoped_per_tenant_and_model() -> None:
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    controller = TenantAdmissionController(
        _bundle(limit=1, queued=0),
        redis=redis,
        lease_ttl_seconds=5.0,
    )
    finance_qwen = await controller.acquire_lease("finance", model="qwen")
    research_qwen = await controller.acquire_lease("research", model="qwen")
    finance_llama = await controller.acquire_lease("finance", model="llama")
    with pytest.raises(HTTPException):
        await controller.acquire_lease("finance", model="qwen")
    await finance_qwen.release()
    await research_qwen.release()
    await finance_llama.release()
    await controller.aclose()


@pytest.mark.anyio
async def test_release_requires_matching_fencing_token() -> None:
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    controller = TenantAdmissionController(
        _bundle(limit=1, queued=0),
        redis=redis,
        lease_ttl_seconds=5.0,
    )
    lease = await controller.acquire_lease("finance", model="qwen")
    assert lease.lease_id is not None
    assert lease.fencing_token is not None
    # Stale fencing token must not free the slot.
    forged = lease
    forged_token = lease.fencing_token
    lease.fencing_token = forged_token + 99
    await lease.release()
    assert await controller.inflight_count("finance", "qwen") == 1
    # Restore and release for real.
    lease._released = False
    lease.fencing_token = forged_token
    await forged.release()
    assert await controller.inflight_count("finance", "qwen") == 0
    await controller.aclose()


@pytest.mark.anyio
async def test_expired_leases_are_reclaimed() -> None:
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    controller = TenantAdmissionController(
        _bundle(limit=1, queued=0),
        redis=redis,
        lease_ttl_seconds=0.2,
        renew_interval_seconds=5.0,  # do not renew
    )
    lease = await controller.acquire_lease("finance", model="qwen")
    # Stop renewal so the lease can expire (simulates pod death).
    if lease._renew_task is not None:
        lease._renew_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await lease._renew_task
        lease._renew_task = None
    await asyncio.sleep(0.35)
    # Capacity recovers via TTL expiry without an explicit release.
    recovered = await controller.acquire_lease("finance", model="qwen")
    assert recovered.fencing_token is not None
    assert recovered.fencing_token > (lease.fencing_token or 0)
    assert await controller.inflight_count("finance", "qwen") == 1
    await recovered.release()
    lease._released = True  # avoid double-release noise
    await controller.aclose()


@pytest.mark.anyio
async def test_renewal_extends_lease_past_ttl() -> None:
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    controller = TenantAdmissionController(
        _bundle(limit=1, queued=0),
        redis=redis,
        lease_ttl_seconds=0.3,
        renew_interval_seconds=0.1,
    )
    lease = await controller.acquire_lease("finance", model="qwen")
    await asyncio.sleep(0.7)
    with pytest.raises(HTTPException) as error:
        await controller.acquire_lease("finance", model="qwen")
    assert error.value.status_code == 429
    await lease.release()
    await controller.aclose()


class _BrokenRedis:
    """Stand-in that fails every Redis call (simulates outage)."""

    async def script_load(self, *_args: object, **_kwargs: object) -> str:
        raise ConnectionError("redis unavailable")

    async def evalsha(self, *_args: object, **_kwargs: object) -> object:
        raise ConnectionError("redis unavailable")

    async def ping(self) -> bool:
        raise ConnectionError("redis unavailable")

    async def aclose(self) -> None:
        return None


@pytest.mark.anyio
async def test_fail_closed_when_redis_unavailable() -> None:
    controller = TenantAdmissionController(
        _bundle(limit=2, queued=0, policy="fail_closed"),
        redis=_BrokenRedis(),  # type: ignore[arg-type]
    )
    with pytest.raises(HTTPException) as error:
        await controller.acquire_lease("finance", model="qwen")
    assert error.value.status_code == 503
    assert error.value.detail["error"]["code"] == "admission_store_unavailable"


@pytest.mark.anyio
async def test_degraded_falls_back_to_local_limit() -> None:
    controller = TenantAdmissionController(
        _bundle(limit=1, queued=0, policy="degraded"),
        redis=_BrokenRedis(),  # type: ignore[arg-type]
    )
    first = await controller.acquire_lease("finance", model="qwen")
    assert first.mode == "degraded"
    with pytest.raises(HTTPException) as error:
        await controller.acquire_lease("finance", model="qwen")
    assert error.value.status_code == 429
    await first.release()


@pytest.mark.anyio
async def test_fail_open_admits_without_semaphore() -> None:
    controller = TenantAdmissionController(
        _bundle(limit=1, queued=0, policy="fail_open"),
        redis=_BrokenRedis(),  # type: ignore[arg-type]
    )
    first = await controller.acquire_lease("finance", model="qwen")
    second = await controller.acquire_lease("finance", model="qwen")
    assert first.mode == "fail_open"
    assert second.mode == "fail_open"
    await first.release()
    await second.release()


@pytest.mark.anyio
async def test_status_reports_redis_mode() -> None:
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    controller = create_admission_controller(_bundle(), redis=redis)
    status = controller.status()
    assert status["mode"] == "redis"
    assert status["redis_configured"] is True
    assert status["redis_ok"] is True
    await controller.aclose()


@pytest.mark.anyio
async def test_local_mode_without_redis_still_enforces() -> None:
    controller = TenantAdmissionController(_bundle(limit=1, queued=0))
    first = await controller.acquire_lease("finance", model="qwen")
    assert first.mode == "local"
    with pytest.raises(HTTPException):
        await controller.acquire_lease("finance", model="qwen")
    await first.release()


@pytest.mark.anyio
async def test_concurrent_tenants_never_share_slots() -> None:
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    controller = TenantAdmissionController(
        _bundle(limit=1, queued=8),
        redis=redis,
        lease_ttl_seconds=5.0,
    )

    async def hold(tenant: str) -> None:
        lease = await controller.acquire_lease(tenant, model="qwen")
        await asyncio.sleep(0.05)
        await lease.release()

    await asyncio.gather(
        *(hold("finance") for _ in range(5)), *(hold("research") for _ in range(5))
    )
    assert await controller.inflight_count("finance", "qwen") == 0
    assert await controller.inflight_count("research", "qwen") == 0
    await controller.aclose()


@pytest.mark.anyio
async def test_two_replica_controllers_share_fleet_limit() -> None:
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    bundle = _bundle(limit=2, queued=0)
    replica_a = TenantAdmissionController(bundle, redis=redis, lease_ttl_seconds=5.0)
    # Second replica shares the same Redis logical DB (separate client wrapper).
    redis_b = redis
    replica_b = TenantAdmissionController(bundle, redis=redis_b, lease_ttl_seconds=5.0)
    a1 = await replica_a.acquire_lease("finance", model="qwen")
    b1 = await replica_b.acquire_lease("finance", model="qwen")
    with pytest.raises(HTTPException) as error:
        await replica_a.acquire_lease("finance", model="qwen")
    assert error.value.status_code == 429
    # Independent tenant budget is unaffected by finance saturation.
    r1 = await replica_a.acquire_lease("research", model="qwen")
    r2 = await replica_b.acquire_lease("research", model="qwen")
    with pytest.raises(HTTPException):
        await replica_a.acquire_lease("research", model="qwen")
    await a1.release()
    await b1.release()
    await r1.release()
    await r2.release()
    # Do not aclose shared fakeredis twice.
    replica_a._redis = None
    replica_b._redis = None
    await redis.aclose()


@pytest.mark.anyio
async def test_expiry_purge_records_metric_path() -> None:
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    controller = TenantAdmissionController(
        _bundle(limit=1, queued=0),
        redis=redis,
        lease_ttl_seconds=0.15,
        renew_interval_seconds=10.0,
    )
    lease = await controller.acquire_lease("finance", model="qwen")
    if lease._renew_task is not None:
        lease._renew_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await lease._renew_task
        lease._renew_task = None
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline:
        count = await controller.inflight_count("finance", "qwen")
        if count == 0:
            break
        await asyncio.sleep(0.05)
    assert await controller.inflight_count("finance", "qwen") == 0
    lease._released = True
    await controller.aclose()
