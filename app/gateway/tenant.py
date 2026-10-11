"""Tenant attribution counters for governance quota signals."""

from __future__ import annotations

import contextlib
import os
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Protocol

from prometheus_client import Counter

if TYPE_CHECKING:
    from redis.asyncio import Redis

TENANT_REQUESTS = Counter(
    "gateway_tenant_requests_total",
    "Chat completion requests attributed to a tenant team.",
    ["team"],
)
TENANT_TOKENS = Counter(
    "gateway_tenant_tokens_total",
    "Estimated tokens attributed to a tenant team.",
    ["team"],
)
TENANT_SPEND = Counter(
    "gateway_tenant_spend_usd_total",
    "Attributed spend USD for a tenant team (from model pricing).",
    ["team"],
)

REDIS_KEY_PREFIX = "arp:"
WINDOW_SECONDS = 60

# Atomic sliding-window RPM (ZSET) + day tokens/spend hash.
# KEYS[1]=rpm zset, KEYS[2]=usage hash
# ARGV: now, window_sec, member, token_delta, today, spend_delta
_RESERVE_LUA = """
local now = tonumber(ARGV[1])
local window = tonumber(ARGV[2])
local member = ARGV[3]
local token_delta = tonumber(ARGV[4])
local today = ARGV[5]
local spend_delta = tonumber(ARGV[6])

redis.call('ZREMRANGEBYSCORE', KEYS[1], '-inf', now - window)
redis.call('ZADD', KEYS[1], now, member)
redis.call('PEXPIRE', KEYS[1], (window + 1) * 1000)
local rpm = redis.call('ZCARD', KEYS[1])

local day = redis.call('HGET', KEYS[2], 'day')
if day ~= today then
  redis.call('HSET', KEYS[2], 'day', today, 'tokens', '0', 'spend_usd', '0')
end
local tokens_today = redis.call('HINCRBY', KEYS[2], 'tokens', token_delta)
local spend = redis.call('HINCRBYFLOAT', KEYS[2], 'spend_usd', spend_delta)
redis.call('EXPIRE', KEYS[2], 172800)

return {rpm, tokens_today, tostring(spend)}
"""

# Refund tokens/spend and remove one RPM member (best-effort by member id).
# KEYS[1]=rpm zset, KEYS[2]=usage hash
# ARGV: member, token_delta, today, spend_delta
_REFUND_LUA = """
local member = ARGV[1]
local token_delta = tonumber(ARGV[2])
local today = ARGV[3]
local spend_delta = tonumber(ARGV[4])

redis.call('ZREM', KEYS[1], member)

local day = redis.call('HGET', KEYS[2], 'day')
if day == today then
  local tokens = tonumber(redis.call('HGET', KEYS[2], 'tokens') or '0')
  local next_tokens = tokens - token_delta
  if next_tokens < 0 then next_tokens = 0 end
  redis.call('HSET', KEYS[2], 'tokens', tostring(next_tokens))
  local spend = tonumber(redis.call('HGET', KEYS[2], 'spend_usd') or '0')
  local next_spend = spend - spend_delta
  if next_spend < 0 then next_spend = 0 end
  redis.call('HSET', KEYS[2], 'spend_usd', tostring(next_spend))
end
return 1
"""

# Reconcile reserved estimate vs actual tokens/spend (delta adjustment).
# KEYS[1]=usage hash
# ARGV: token_delta, today, spend_delta
_RECONCILE_LUA = """
local token_delta = tonumber(ARGV[1])
local today = ARGV[2]
local spend_delta = tonumber(ARGV[3])

local day = redis.call('HGET', KEYS[1], 'day')
if day ~= today then
  redis.call('HSET', KEYS[1], 'day', today, 'tokens', '0', 'spend_usd', '0')
end
local tokens_today = redis.call('HINCRBY', KEYS[1], 'tokens', token_delta)
if tokens_today < 0 then
  redis.call('HSET', KEYS[1], 'tokens', '0')
  tokens_today = 0
end
local spend = redis.call('HINCRBYFLOAT', KEYS[1], 'spend_usd', spend_delta)
if tonumber(spend) < 0 then
  redis.call('HSET', KEYS[1], 'spend_usd', '0')
  spend = '0'
end
redis.call('EXPIRE', KEYS[1], 172800)
return {tokens_today, tostring(spend)}
"""


@dataclass
class TenantUsage:
    request_times: deque[float] = field(default_factory=deque)
    tokens_today: int = 0
    spend_usd: float = 0.0
    tokens_day: str = ""


@dataclass(frozen=True)
class QuotaReservation:
    """Pre-request quota reservation that can be refunded or reconciled."""

    team: str
    member_id: str
    reserved_tokens: int
    reserved_spend_usd: float
    requests_last_minute: int
    tokens_today: int
    month_to_date_cost_usd: float


class TenantAttributionBackend(Protocol):
    def resolve_team(self, request) -> str: ...

    async def reserve_request(
        self,
        team: str,
        *,
        input_tokens: int,
        output_tokens: int,
        spend_usd: float = 0.0,
        member_id: str | None = None,
        now: float | None = None,
    ) -> QuotaReservation: ...

    async def record_request(self, team: str, *, input_tokens: int, output_tokens: int) -> None: ...

    async def refund_reservation(self, reservation: QuotaReservation) -> None: ...

    async def reconcile_usage(
        self,
        reservation: QuotaReservation,
        *,
        actual_input_tokens: int,
        actual_output_tokens: int,
        actual_spend_usd: float,
    ) -> tuple[int, float]: ...

    async def usage_snapshot(self, team: str, *, now: float | None = None) -> tuple[int, int]: ...

    async def spend_snapshot(self, team: str) -> float: ...

    async def aclose(self) -> None: ...


def _today() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%d")


def _prune_window(times: deque[float], now: float, window: float = WINDOW_SECONDS) -> None:
    cutoff = now - window
    while times and times[0] < cutoff:
        times.popleft()


class TenantAttributionStore:
    """In-memory per-process counters suitable for single-replica demos."""

    def __init__(self) -> None:
        self._usage: dict[str, TenantUsage] = {}
        self._members: dict[str, float] = {}

    @staticmethod
    def enabled_from_environment() -> bool:
        return os.getenv("TENANT_ATTRIBUTION_ENABLED", "false").strip().lower() in {
            "1",
            "true",
            "yes",
        }

    def resolve_team(self, request) -> str:
        from app.gateway.tenant_context import resolve_tenant_id

        return resolve_tenant_id(request)

    def _get(self, team: str, *, now: float | None = None) -> TenantUsage:
        usage = self._usage.setdefault(team, TenantUsage())
        clock = time.monotonic() if now is None else now
        _prune_window(usage.request_times, clock)
        today = _today()
        if usage.tokens_day != today:
            usage.tokens_day = today
            usage.tokens_today = 0
            usage.spend_usd = 0.0
        return usage

    async def reserve_request(
        self,
        team: str,
        *,
        input_tokens: int,
        output_tokens: int,
        spend_usd: float = 0.0,
        member_id: str | None = None,
        now: float | None = None,
    ) -> QuotaReservation:
        clock = time.monotonic() if now is None else now
        usage = self._get(team, now=clock)
        mid = member_id or str(uuid.uuid4())
        token_delta = input_tokens + output_tokens
        usage.request_times.append(clock)
        self._members[mid] = clock
        usage.tokens_today += token_delta
        usage.spend_usd += spend_usd
        TENANT_REQUESTS.labels(team=team).inc()
        TENANT_TOKENS.labels(team=team).inc(token_delta)
        if spend_usd:
            TENANT_SPEND.labels(team=team).inc(spend_usd)
        return QuotaReservation(
            team=team,
            member_id=mid,
            reserved_tokens=token_delta,
            reserved_spend_usd=spend_usd,
            requests_last_minute=len(usage.request_times),
            tokens_today=usage.tokens_today,
            month_to_date_cost_usd=usage.spend_usd,
        )

    async def record_request(self, team: str, *, input_tokens: int, output_tokens: int) -> None:
        await self.reserve_request(team, input_tokens=input_tokens, output_tokens=output_tokens)

    async def refund_reservation(self, reservation: QuotaReservation) -> None:
        usage = self._get(reservation.team)
        if reservation.member_id in self._members:
            # Remove one matching timestamp from the sliding window.
            stamp = self._members.pop(reservation.member_id)
            with contextlib.suppress(ValueError):
                usage.request_times.remove(stamp)
        usage.tokens_today = max(0, usage.tokens_today - reservation.reserved_tokens)
        usage.spend_usd = max(0.0, usage.spend_usd - reservation.reserved_spend_usd)

    async def reconcile_usage(
        self,
        reservation: QuotaReservation,
        *,
        actual_input_tokens: int,
        actual_output_tokens: int,
        actual_spend_usd: float,
    ) -> tuple[int, float]:
        usage = self._get(reservation.team)
        actual_tokens = actual_input_tokens + actual_output_tokens
        token_delta = actual_tokens - reservation.reserved_tokens
        spend_delta = actual_spend_usd - reservation.reserved_spend_usd
        usage.tokens_today = max(0, usage.tokens_today + token_delta)
        usage.spend_usd = max(0.0, usage.spend_usd + spend_delta)
        if token_delta > 0:
            TENANT_TOKENS.labels(team=reservation.team).inc(token_delta)
        if spend_delta > 0:
            TENANT_SPEND.labels(team=reservation.team).inc(spend_delta)
        return usage.tokens_today, usage.spend_usd

    async def usage_snapshot(self, team: str, *, now: float | None = None) -> tuple[int, int]:
        usage = self._get(team, now=now)
        return len(usage.request_times), usage.tokens_today

    async def spend_snapshot(self, team: str) -> float:
        usage = self._get(team)
        return usage.spend_usd

    async def aclose(self) -> None:
        return None


class RedisTenantAttributionStore:
    """Fleet-wide tenant counters shared across gateway replicas (atomic Lua)."""

    def __init__(self, redis: Redis) -> None:
        self._redis = redis
        self._reserve_sha: str | None = None
        self._refund_sha: str | None = None
        self._reconcile_sha: str | None = None

    def resolve_team(self, request) -> str:
        from app.gateway.tenant_context import resolve_tenant_id

        return resolve_tenant_id(request)

    def _rpm_key(self, team: str) -> str:
        return f"{REDIS_KEY_PREFIX}{team}:rpm"

    def _usage_key(self, team: str) -> str:
        return f"{REDIS_KEY_PREFIX}{team}:usage"

    async def _ensure_scripts(self) -> None:
        if self._reserve_sha is None:
            self._reserve_sha = await self._redis.script_load(_RESERVE_LUA)
        if self._refund_sha is None:
            self._refund_sha = await self._redis.script_load(_REFUND_LUA)
        if self._reconcile_sha is None:
            self._reconcile_sha = await self._redis.script_load(_RECONCILE_LUA)

    async def _eval(
        self, sha_attr: str, script: str, keys: list[str], args: list[object]
    ) -> object:
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

    async def reserve_request(
        self,
        team: str,
        *,
        input_tokens: int,
        output_tokens: int,
        spend_usd: float = 0.0,
        member_id: str | None = None,
        now: float | None = None,
    ) -> QuotaReservation:
        clock = time.time() if now is None else now
        mid = member_id or str(uuid.uuid4())
        token_delta = input_tokens + output_tokens
        result = await self._eval(
            "_reserve_sha",
            _RESERVE_LUA,
            [self._rpm_key(team), self._usage_key(team)],
            [clock, WINDOW_SECONDS, mid, token_delta, _today(), spend_usd],
        )
        rpm = int(result[0])
        tokens_today = int(result[1])
        spend = float(result[2])
        TENANT_REQUESTS.labels(team=team).inc()
        TENANT_TOKENS.labels(team=team).inc(token_delta)
        if spend_usd:
            TENANT_SPEND.labels(team=team).inc(spend_usd)
        return QuotaReservation(
            team=team,
            member_id=mid,
            reserved_tokens=token_delta,
            reserved_spend_usd=spend_usd,
            requests_last_minute=rpm,
            tokens_today=tokens_today,
            month_to_date_cost_usd=spend,
        )

    async def record_request(self, team: str, *, input_tokens: int, output_tokens: int) -> None:
        await self.reserve_request(team, input_tokens=input_tokens, output_tokens=output_tokens)

    async def refund_reservation(self, reservation: QuotaReservation) -> None:
        await self._eval(
            "_refund_sha",
            _REFUND_LUA,
            [self._rpm_key(reservation.team), self._usage_key(reservation.team)],
            [
                reservation.member_id,
                reservation.reserved_tokens,
                _today(),
                reservation.reserved_spend_usd,
            ],
        )

    async def reconcile_usage(
        self,
        reservation: QuotaReservation,
        *,
        actual_input_tokens: int,
        actual_output_tokens: int,
        actual_spend_usd: float,
    ) -> tuple[int, float]:
        actual_tokens = actual_input_tokens + actual_output_tokens
        token_delta = actual_tokens - reservation.reserved_tokens
        spend_delta = actual_spend_usd - reservation.reserved_spend_usd
        result = await self._eval(
            "_reconcile_sha",
            _RECONCILE_LUA,
            [self._usage_key(reservation.team)],
            [token_delta, _today(), spend_delta],
        )
        tokens_today = int(result[0])
        spend = float(result[1])
        if token_delta > 0:
            TENANT_TOKENS.labels(team=reservation.team).inc(token_delta)
        if spend_delta > 0:
            TENANT_SPEND.labels(team=reservation.team).inc(spend_delta)
        return tokens_today, spend

    async def usage_snapshot(self, team: str, *, now: float | None = None) -> tuple[int, int]:
        clock = time.time() if now is None else now
        rpm_key = self._rpm_key(team)
        await self._redis.zremrangebyscore(rpm_key, "-inf", clock - WINDOW_SECONDS)
        rpm = int(await self._redis.zcard(rpm_key) or 0)
        state = await self._redis.hgetall(self._usage_key(team))
        if not state:
            return rpm, 0
        tokens_today = int(state.get("tokens", 0) or 0)
        if (state.get("day") or "") != _today():
            tokens_today = 0
        return rpm, tokens_today

    async def spend_snapshot(self, team: str) -> float:
        state = await self._redis.hgetall(self._usage_key(team))
        if not state or (state.get("day") or "") != _today():
            return 0.0
        return float(state.get("spend_usd", 0) or 0)

    async def aclose(self) -> None:
        await self._redis.aclose()


def create_tenant_store(redis_url: str | None) -> TenantAttributionBackend | None:
    if not TenantAttributionStore.enabled_from_environment():
        return None
    if redis_url:
        from redis.asyncio import Redis

        redis = Redis.from_url(redis_url, decode_responses=True)
        return RedisTenantAttributionStore(redis)
    return TenantAttributionStore()
