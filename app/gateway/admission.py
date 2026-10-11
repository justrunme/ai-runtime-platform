"""Tenant admission control with optional Redis fleet-wide semaphore."""

from __future__ import annotations

import asyncio
import os
import time
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal

from fastapi import HTTPException
from prometheus_client import Counter, Gauge, Histogram

from app.gateway.tenant_policy import (
    RedisUnavailablePolicy,
    TenantPolicyBundle,
    load_tenant_policy_bundle,
)

if TYPE_CHECKING:
    from redis.asyncio import Redis

AdmissionMode = Literal["local", "redis", "fail_open", "degraded"]

ADMISSION_INFLIGHT = Gauge(
    "gateway_admission_inflight",
    "In-flight admitted requests held by the concurrency semaphore.",
    ["tenant", "model"],
)
ADMISSION_ACQUIRE_LATENCY = Histogram(
    "gateway_admission_acquire_seconds",
    "Latency to acquire a tenant admission lease.",
    ["tenant", "mode"],
    buckets=(0.001, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0),
)
ADMISSION_REJECTIONS = Counter(
    "gateway_admission_rejections_total",
    "Admission rejections by tenant and reason.",
    ["tenant", "reason"],
)
ADMISSION_LEASES_EXPIRED = Counter(
    "gateway_admission_leases_expired_total",
    "Leases purged after TTL expiry (pod death / missed renewal).",
    ["tenant"],
)
ADMISSION_REDIS_ERRORS = Counter(
    "gateway_admission_redis_errors_total",
    "Redis errors during fleet admission operations.",
    ["operation"],
)

# KEYS[1]=leases zset (member=lease_id, score=expiry_ms)
# KEYS[2]=tokens hash (lease_id -> fencing token)
# KEYS[3]=fence counter
# ARGV: now_ms, ttl_ms, limit, lease_id
_ACQUIRE_LUA = """
local now = tonumber(ARGV[1])
local ttl = tonumber(ARGV[2])
local limit = tonumber(ARGV[3])
local lease_id = ARGV[4]

local expired = redis.call('ZRANGEBYSCORE', KEYS[1], '-inf', now)
local expired_count = #expired
if expired_count > 0 then
  for i = 1, expired_count do
    redis.call('HDEL', KEYS[2], expired[i])
  end
  redis.call('ZREMRANGEBYSCORE', KEYS[1], '-inf', now)
end

local inflight = redis.call('ZCARD', KEYS[1])
if inflight >= limit then
  return {0, inflight, expired_count}
end

local token = redis.call('INCR', KEYS[3])
redis.call('ZADD', KEYS[1], now + ttl, lease_id)
redis.call('HSET', KEYS[2], lease_id, tostring(token))
redis.call('PEXPIRE', KEYS[1], ttl * 4)
redis.call('PEXPIRE', KEYS[2], ttl * 4)
redis.call('PEXPIRE', KEYS[3], ttl * 8)
return {1, token, inflight + 1, expired_count}
"""

# KEYS[1]=leases zset, KEYS[2]=tokens hash
# ARGV: now_ms, ttl_ms, lease_id, fencing_token
_RENEW_LUA = """
local now = tonumber(ARGV[1])
local ttl = tonumber(ARGV[2])
local lease_id = ARGV[3]
local token = ARGV[4]

local stored = redis.call('HGET', KEYS[2], lease_id)
if stored ~= token then
  return 0
end
local score = redis.call('ZSCORE', KEYS[1], lease_id)
if not score then
  return 0
end
redis.call('ZADD', KEYS[1], now + ttl, lease_id)
redis.call('PEXPIRE', KEYS[1], ttl * 4)
redis.call('PEXPIRE', KEYS[2], ttl * 4)
return 1
"""

# KEYS[1]=leases zset, KEYS[2]=tokens hash
# ARGV: lease_id, fencing_token
_RELEASE_LUA = """
local lease_id = ARGV[1]
local token = ARGV[2]
local stored = redis.call('HGET', KEYS[2], lease_id)
if stored ~= token then
  return 0
end
redis.call('ZREM', KEYS[1], lease_id)
redis.call('HDEL', KEYS[2], lease_id)
return 1
"""

# KEYS[1]=leases zset, KEYS[2]=tokens hash
# ARGV: now_ms
_PURGE_LUA = """
local now = tonumber(ARGV[1])
local expired = redis.call('ZRANGEBYSCORE', KEYS[1], '-inf', now)
local expired_count = #expired
if expired_count > 0 then
  for i = 1, expired_count do
    redis.call('HDEL', KEYS[2], expired[i])
  end
  redis.call('ZREMRANGEBYSCORE', KEYS[1], '-inf', now)
end
return {expired_count, redis.call('ZCARD', KEYS[1])}
"""


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    return max(0.1, float(raw))


def _concurrency_exceeded(tenant_id: str, limit: int) -> HTTPException:
    ADMISSION_REJECTIONS.labels(tenant=tenant_id, reason="concurrency_exceeded").inc()
    return HTTPException(
        status_code=429,
        detail={
            "error": {
                "type": "rate_limit_error",
                "code": "tenant_concurrency_exceeded",
                "message": "tenant concurrent request limit exceeded",
            }
        },
        headers={
            "Retry-After": "1",
            "X-RateLimit-Limit": str(limit),
            "X-RateLimit-Remaining": "0",
        },
    )


def _store_unavailable(tenant_id: str) -> HTTPException:
    ADMISSION_REJECTIONS.labels(tenant=tenant_id, reason="store_unavailable").inc()
    return HTTPException(
        status_code=503,
        detail={
            "error": {
                "type": "api_error",
                "code": "admission_store_unavailable",
                "message": (
                    "fleet admission store unavailable; "
                    "tenant policy is fail-closed for Redis outages"
                ),
            }
        },
        headers={"Retry-After": "1"},
    )


@dataclass
class AdmissionLease:
    controller: TenantAdmissionController
    tenant_id: str
    model: str = "*"
    lease_id: str | None = None
    fencing_token: int | None = None
    mode: AdmissionMode = "local"
    _released: bool = False
    _renew_task: asyncio.Task[None] | None = field(default=None, repr=False)

    async def release(self) -> None:
        if self._released:
            return
        self._released = True
        if self._renew_task is not None:
            self._renew_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._renew_task
            self._renew_task = None
        await self.controller._release(self)

    def start_renewal(self) -> None:
        if self.mode != "redis" or self._renew_task is not None:
            return
        self._renew_task = asyncio.create_task(self.controller._renew_loop(self))


class TenantAdmissionController:
    """Admit requests under per-tenant (and per-model) concurrency limits.

    When Redis is configured, limits are fleet-wide via a distributed semaphore.
    When Redis is unavailable, behaviour follows the tenant
    ``redisUnavailablePolicy``: fail-closed (default), degraded (local fallback),
    or fail-open.
    """

    def __init__(
        self,
        bundle: TenantPolicyBundle | None = None,
        *,
        redis: Redis | None = None,
        lease_ttl_seconds: float | None = None,
        renew_interval_seconds: float | None = None,
    ) -> None:
        self._bundle = bundle or load_tenant_policy_bundle()
        self._redis = redis
        self._lease_ttl_seconds = lease_ttl_seconds or _env_float(
            "ADMISSION_LEASE_TTL_SECONDS", 30.0
        )
        self._renew_interval_seconds = renew_interval_seconds or _env_float(
            "ADMISSION_LEASE_RENEW_INTERVAL_SECONDS", 10.0
        )
        self._semaphores: dict[str, asyncio.Semaphore] = {}
        self._queued: dict[str, int] = {}
        self._inflight: dict[str, int] = {}
        self._lock = asyncio.Lock()
        self._acquire_sha: str | None = None
        self._renew_sha: str | None = None
        self._release_sha: str | None = None
        self._purge_sha: str | None = None
        self._redis_ok = redis is not None
        self._last_redis_error: str | None = None

    @property
    def redis_configured(self) -> bool:
        return self._redis is not None

    def status(self) -> dict[str, Any]:
        mode: AdmissionMode
        if self._redis is None:
            mode = "local"
        elif self._redis_ok:
            mode = "redis"
        else:
            mode = "degraded"
        return {
            "mode": mode,
            "redis_configured": self._redis is not None,
            "redis_ok": self._redis_ok if self._redis is not None else None,
            "lease_ttl_seconds": self._lease_ttl_seconds,
            "renew_interval_seconds": self._renew_interval_seconds,
            "last_redis_error": self._last_redis_error,
        }

    def _scope_key(self, tenant_id: str, model: str) -> str:
        return f"{tenant_id}:{model}"

    def _leases_key(self, tenant_id: str, model: str) -> str:
        return f"arp:{tenant_id}:admission:{model}:leases"

    def _tokens_key(self, tenant_id: str, model: str) -> str:
        return f"arp:{tenant_id}:admission:{model}:tokens"

    def _fence_key(self, tenant_id: str, model: str) -> str:
        return f"arp:{tenant_id}:admission:{model}:fence"

    def _local_semaphore(self, tenant_id: str, model: str) -> asyncio.Semaphore:
        policy = self._bundle.for_tenant(tenant_id)
        key = self._scope_key(tenant_id, model)
        if key not in self._semaphores:
            self._semaphores[key] = asyncio.Semaphore(policy.max_concurrent_requests)
        return self._semaphores[key]

    async def _ensure_scripts(self) -> None:
        assert self._redis is not None
        if self._acquire_sha is None:
            self._acquire_sha = await self._redis.script_load(_ACQUIRE_LUA)
        if self._renew_sha is None:
            self._renew_sha = await self._redis.script_load(_RENEW_LUA)
        if self._release_sha is None:
            self._release_sha = await self._redis.script_load(_RELEASE_LUA)
        if self._purge_sha is None:
            self._purge_sha = await self._redis.script_load(_PURGE_LUA)

    async def _eval(
        self, sha_attr: str, script: str, keys: list[str], args: list[object]
    ) -> object:
        assert self._redis is not None
        await self._ensure_scripts()
        sha = getattr(self, sha_attr)
        try:
            return await self._redis.evalsha(sha, len(keys), *keys, *args)
        except Exception as error:  # noqa: BLE001 - NOSCRIPT reload path
            if "NOSCRIPT" not in str(error).upper():
                raise
            sha = await self._redis.script_load(script)
            setattr(self, sha_attr, sha)
            return await self._redis.evalsha(sha, len(keys), *keys, *args)

    def _record_expired(self, tenant_id: str, expired_count: int) -> None:
        if expired_count > 0:
            ADMISSION_LEASES_EXPIRED.labels(tenant=tenant_id).inc(expired_count)

    async def ping(self) -> bool:
        if self._redis is None:
            return True
        try:
            await self._redis.ping()
            self._redis_ok = True
            self._last_redis_error = None
            return True
        except Exception as error:  # noqa: BLE001
            self._redis_ok = False
            self._last_redis_error = str(error)
            return False

    async def inflight_count(self, tenant_id: str, model: str = "*") -> int:
        if self._redis is not None and self._redis_ok:
            try:
                now_ms = int(time.time() * 1000)
                result = await self._eval(
                    "_purge_sha",
                    _PURGE_LUA,
                    [self._leases_key(tenant_id, model), self._tokens_key(tenant_id, model)],
                    [now_ms],
                )
                expired = int(result[0])
                inflight = int(result[1])
                self._record_expired(tenant_id, expired)
                ADMISSION_INFLIGHT.labels(tenant=tenant_id, model=model).set(inflight)
                return inflight
            except Exception as error:  # noqa: BLE001
                ADMISSION_REDIS_ERRORS.labels(operation="inflight").inc()
                self._redis_ok = False
                self._last_redis_error = str(error)
        key = self._scope_key(tenant_id, model)
        return self._inflight.get(key, 0)

    async def acquire_lease(self, tenant_id: str, model: str | None = None) -> AdmissionLease:
        model_key = str(model or "*")
        policy = self._bundle.for_tenant(tenant_id)
        started = time.perf_counter()
        try:
            if self._redis is not None:
                try:
                    lease = await self._acquire_redis(tenant_id, model_key, policy)
                    ADMISSION_ACQUIRE_LATENCY.labels(tenant=tenant_id, mode="redis").observe(
                        time.perf_counter() - started
                    )
                    lease.start_renewal()
                    return lease
                except HTTPException:
                    raise
                except Exception as error:  # noqa: BLE001 - apply unavailable policy
                    ADMISSION_REDIS_ERRORS.labels(operation="acquire").inc()
                    self._redis_ok = False
                    self._last_redis_error = str(error)
                    return await self._acquire_unavailable(
                        tenant_id, model_key, policy, started, error
                    )
            lease = await self._acquire_local(tenant_id, model_key, policy)
            ADMISSION_ACQUIRE_LATENCY.labels(tenant=tenant_id, mode="local").observe(
                time.perf_counter() - started
            )
            return lease
        except HTTPException:
            raise

    async def _acquire_unavailable(
        self,
        tenant_id: str,
        model: str,
        policy: Any,
        started: float,
        error: Exception,
    ) -> AdmissionLease:
        unavailable: RedisUnavailablePolicy = policy.redis_unavailable_policy
        if unavailable == "fail_closed":
            raise _store_unavailable(tenant_id) from error
        if unavailable == "fail_open":
            ADMISSION_ACQUIRE_LATENCY.labels(tenant=tenant_id, mode="fail_open").observe(
                time.perf_counter() - started
            )
            return AdmissionLease(
                controller=self,
                tenant_id=tenant_id,
                model=model,
                mode="fail_open",
            )
        # degraded: local process limit
        lease = await self._acquire_local(tenant_id, model, policy)
        ADMISSION_ACQUIRE_LATENCY.labels(tenant=tenant_id, mode="degraded").observe(
            time.perf_counter() - started
        )
        lease.mode = "degraded"
        return lease

    async def _acquire_redis(self, tenant_id: str, model: str, policy: Any) -> AdmissionLease:
        assert self._redis is not None
        limit = policy.max_concurrent_requests
        max_queued = policy.max_queued_requests
        scope = self._scope_key(tenant_id, model)
        async with self._lock:
            queued = self._queued.get(scope, 0)
            # Probe current fleet occupancy without consuming a slot.
            now_ms = int(time.time() * 1000)
            probe = await self._eval(
                "_purge_sha",
                _PURGE_LUA,
                [self._leases_key(tenant_id, model), self._tokens_key(tenant_id, model)],
                [now_ms],
            )
            self._record_expired(tenant_id, int(probe[0]))
            inflight = int(probe[1])
            if inflight >= limit and queued >= max_queued:
                raise _concurrency_exceeded(tenant_id, limit)
            self._queued[scope] = queued + 1

        acquired = False
        lease: AdmissionLease | None = None
        try:
            # Wait for a fleet slot (local queue bound by max_queued).
            while True:
                lease_id = str(uuid.uuid4())
                now_ms = int(time.time() * 1000)
                ttl_ms = int(self._lease_ttl_seconds * 1000)
                result = await self._eval(
                    "_acquire_sha",
                    _ACQUIRE_LUA,
                    [
                        self._leases_key(tenant_id, model),
                        self._tokens_key(tenant_id, model),
                        self._fence_key(tenant_id, model),
                    ],
                    [now_ms, ttl_ms, limit, lease_id],
                )
                self._record_expired(tenant_id, int(result[-1]))
                if int(result[0]) == 1:
                    token = int(result[1])
                    inflight_now = int(result[2])
                    ADMISSION_INFLIGHT.labels(tenant=tenant_id, model=model).set(inflight_now)
                    self._redis_ok = True
                    self._last_redis_error = None
                    lease = AdmissionLease(
                        controller=self,
                        tenant_id=tenant_id,
                        model=model,
                        lease_id=lease_id,
                        fencing_token=token,
                        mode="redis",
                    )
                    acquired = True
                    return lease
                # Lost the race with no local wait budget — reject immediately.
                if max_queued == 0:
                    raise _concurrency_exceeded(tenant_id, limit)
                # Keep waiting while holding a local queue ticket.
                await asyncio.sleep(0.02)
        finally:
            async with self._lock:
                self._queued[scope] = max(0, self._queued.get(scope, 1) - 1)
                if acquired:
                    self._inflight[scope] = self._inflight.get(scope, 0) + 1

    async def _acquire_local(self, tenant_id: str, model: str, policy: Any) -> AdmissionLease:
        sem = self._local_semaphore(tenant_id, model)
        scope = self._scope_key(tenant_id, model)
        async with self._lock:
            inflight = self._inflight.get(scope, 0)
            queued = self._queued.get(scope, 0)
            if inflight >= policy.max_concurrent_requests and queued >= policy.max_queued_requests:
                raise _concurrency_exceeded(tenant_id, policy.max_concurrent_requests)
            self._queued[scope] = queued + 1
        acquired = False
        try:
            await sem.acquire()
            acquired = True
        finally:
            async with self._lock:
                self._queued[scope] = max(0, self._queued.get(scope, 1) - 1)
                if acquired:
                    self._inflight[scope] = self._inflight.get(scope, 0) + 1
                    ADMISSION_INFLIGHT.labels(tenant=tenant_id, model=model).set(
                        self._inflight[scope]
                    )
        return AdmissionLease(controller=self, tenant_id=tenant_id, model=model, mode="local")

    async def _renew_loop(self, lease: AdmissionLease) -> None:
        interval = min(self._renew_interval_seconds, self._lease_ttl_seconds / 2)
        while not lease._released:
            await asyncio.sleep(interval)
            if lease._released:
                return
            try:
                ok = await self._renew(lease)
                if not ok:
                    return
            except Exception as error:  # noqa: BLE001
                ADMISSION_REDIS_ERRORS.labels(operation="renew").inc()
                self._redis_ok = False
                self._last_redis_error = str(error)
                return

    async def _renew(self, lease: AdmissionLease) -> bool:
        if self._redis is None or lease.lease_id is None or lease.fencing_token is None:
            return False
        now_ms = int(time.time() * 1000)
        ttl_ms = int(self._lease_ttl_seconds * 1000)
        result = await self._eval(
            "_renew_sha",
            _RENEW_LUA,
            [
                self._leases_key(lease.tenant_id, lease.model),
                self._tokens_key(lease.tenant_id, lease.model),
            ],
            [now_ms, ttl_ms, lease.lease_id, str(lease.fencing_token)],
        )
        return int(result) == 1

    async def _release(self, lease: AdmissionLease) -> None:
        scope = self._scope_key(lease.tenant_id, lease.model)
        if lease.mode in {"local", "degraded"}:
            sem = self._local_semaphore(lease.tenant_id, lease.model)
            sem.release()
            async with self._lock:
                self._inflight[scope] = max(0, self._inflight.get(scope, 1) - 1)
                ADMISSION_INFLIGHT.labels(tenant=lease.tenant_id, model=lease.model).set(
                    self._inflight[scope]
                )
            return
        if lease.mode == "fail_open":
            return
        if (
            self._redis is not None
            and lease.lease_id is not None
            and lease.fencing_token is not None
        ):
            try:
                await self._eval(
                    "_release_sha",
                    _RELEASE_LUA,
                    [
                        self._leases_key(lease.tenant_id, lease.model),
                        self._tokens_key(lease.tenant_id, lease.model),
                    ],
                    [lease.lease_id, str(lease.fencing_token)],
                )
                async with self._lock:
                    self._inflight[scope] = max(0, self._inflight.get(scope, 1) - 1)
                with suppress(Exception):
                    await self.inflight_count(lease.tenant_id, lease.model)
            except Exception as error:  # noqa: BLE001
                ADMISSION_REDIS_ERRORS.labels(operation="release").inc()
                self._redis_ok = False
                self._last_redis_error = str(error)

    @asynccontextmanager
    async def acquire(
        self, tenant_id: str, model: str | None = None
    ) -> AsyncIterator[AdmissionLease]:
        lease = await self.acquire_lease(tenant_id, model=model)
        try:
            yield lease
        finally:
            await lease.release()

    async def aclose(self) -> None:
        if self._redis is not None:
            await self._redis.aclose()


def create_admission_controller(
    bundle: TenantPolicyBundle | None = None,
    *,
    redis_url: str | None = None,
    redis: Redis | None = None,
    lease_ttl_seconds: float | None = None,
    renew_interval_seconds: float | None = None,
) -> TenantAdmissionController:
    client = redis
    if client is None and redis_url:
        from redis.asyncio import Redis

        client = Redis.from_url(redis_url, decode_responses=True)
    return TenantAdmissionController(
        bundle,
        redis=client,
        lease_ttl_seconds=lease_ttl_seconds,
        renew_interval_seconds=renew_interval_seconds,
    )
