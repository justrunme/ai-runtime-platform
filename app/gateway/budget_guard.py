"""Inference budget guard: atomic reserve → execute → settle on Redis or memory.

Fits the gateway admission path beside fleet concurrency leases. Concurrent
reservations never overspend the configured window budget (on this store).
Store outages, timeouts, and corrupt data fail closed (deny).
"""

from __future__ import annotations

import asyncio
import math
import os
import time
import uuid
from dataclasses import dataclass, field
from decimal import ROUND_CEILING, ROUND_FLOOR, ROUND_HALF_EVEN, Decimal
from typing import TYPE_CHECKING, Any, Literal

from fastapi import HTTPException
from prometheus_client import Counter, Gauge, Histogram

from app.gateway.budget_policy import (
    BudgetPolicyError,
    InferenceBudgetPolicy,
    ScopeBudget,
    load_inference_budget_policy,
)

if TYPE_CHECKING:
    from redis.asyncio import Redis

Decision = Literal["ALLOW", "DENY"]

BUDGET_RESERVATIONS = Gauge(
    "gateway_budget_reserved_usd",
    "Currently reserved inference spend USD by scope.",
    ["scope"],
)
BUDGET_COMMITTED = Gauge(
    "gateway_budget_committed_usd",
    "Committed (settled) inference spend USD in the active window by scope.",
    ["scope"],
)
BUDGET_DECISIONS = Counter(
    "gateway_budget_decisions_total",
    "Budget guard decisions.",
    ["decision", "reason"],
)
BUDGET_RESERVE_LATENCY = Histogram(
    "gateway_budget_reserve_seconds",
    "Latency of budget reservation attempts.",
    ["backend"],
    buckets=(0.0005, 0.001, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0),
)
BUDGET_SOFT_WARNINGS = Counter(
    "gateway_budget_soft_warnings_total",
    "Reservations that crossed the soft-warning threshold.",
    ["scope"],
)

# All money in Redis is integer USD micro-units. Reservation amounts live in the
# amounts hash as "<micros>" while live and "x<micros>" (tombstone) once expired, so
# a late settle can still commit real spend exactly once.
#
# KEYS[1]=window hash, KEYS[2]=reservations zset (id -> expiry_ms),
# KEYS[3]=reservation amounts hash (id -> micros | x<micros>)
_LUA_PRELUDE = """
local function num(v)
  if v == false or v == nil then return 0 end
  return tonumber(v)
end

-- Returns committed, reserved, window_start or nil when the stored hash is corrupt.
local function load_window()
  local committed = num(redis.call('HGET', KEYS[1], 'committed_micros'))
  local reserved = num(redis.call('HGET', KEYS[1], 'reserved_micros'))
  local window_start = num(redis.call('HGET', KEYS[1], 'window_start'))
  if committed == nil or reserved == nil or window_start == nil then
    return nil
  end
  return committed, reserved, window_start
end

-- Expire reservations whose TTL passed: release their amount from reserved and
-- leave a tombstone. Returns new reserved, expired count, released micros,
-- or nil when a stored amount is corrupt (nothing is modified in that case).
local function expire(now, reserved)
  local expired = redis.call('ZRANGEBYSCORE', KEYS[2], '-inf', now)
  local amounts = {}
  for i = 1, #expired do
    local raw = redis.call('HGET', KEYS[3], expired[i])
    local amt = 0
    if raw then
      amt = tonumber(raw)
      if amt == nil then return nil end
    end
    amounts[i] = amt
  end
  local released = 0
  for i = 1, #expired do
    released = released + amounts[i]
    redis.call('HSET', KEYS[3], expired[i], 'x' .. string.format('%d', amounts[i]))
  end
  if #expired > 0 then
    redis.call('ZREMRANGEBYSCORE', KEYS[2], '-inf', now)
  end
  reserved = reserved - released
  if reserved < 0 then reserved = 0 end
  return reserved, #expired, released
end

local function touch(ttl)
  redis.call('PEXPIRE', KEYS[1], ttl)
  redis.call('PEXPIRE', KEYS[2], ttl)
  redis.call('PEXPIRE', KEYS[3], ttl)
end
"""

# ARGV: now_ms, window_ms, budget_micros, estimate_micros, res_id, ttl_ms
# Returns {code, committed, reserved, total, expired}; code 1=allow, 0=deny, -1=bad args,
# -2=corrupt store.
_RESERVE_LUA = (
    _LUA_PRELUDE
    + """
local now = tonumber(ARGV[1])
local window = tonumber(ARGV[2])
local budget = tonumber(ARGV[3])
local estimate = tonumber(ARGV[4])
local res_id = ARGV[5]
local ttl = tonumber(ARGV[6])
if budget == nil or estimate == nil or now == nil or window == nil or ttl == nil then
  return {-1, 0, 0, 0, 0}
end
if estimate < 0 or budget <= 0 or window <= 0 or ttl <= 0 then
  return {-1, 0, 0, 0, 0}
end
local keys_ttl = math.max(window, ttl) * 4

local committed, reserved, window_start = load_window()
if committed == nil then return {-2, 0, 0, 0, 0} end
local expired_count
reserved, expired_count = expire(now, reserved)
if reserved == nil then return {-2, 0, 0, 0, 0} end

-- Window rollover resets committed spend only; in-flight reservations carry over.
if window_start == 0 or (now - window_start) >= window then
  window_start = now
  committed = 0
end

if (committed + reserved + estimate) > budget then
  redis.call('HSET', KEYS[1], 'window_start', string.format('%d', window_start),
    'committed_micros', string.format('%d', committed),
    'reserved_micros', string.format('%d', reserved))
  touch(keys_ttl)
  return {0, committed, reserved, committed + reserved, expired_count}
end

reserved = reserved + estimate
redis.call('HSET', KEYS[1], 'window_start', string.format('%d', window_start),
  'committed_micros', string.format('%d', committed),
  'reserved_micros', string.format('%d', reserved))
redis.call('ZADD', KEYS[2], now + ttl, res_id)
redis.call('HSET', KEYS[3], res_id, string.format('%d', estimate))
touch(keys_ttl)
return {1, committed, reserved, committed + reserved, expired_count}
"""
)

# ARGV: res_id, actual_micros, now_ms, window_ms
# Returns {code, committed, reserved, reserved_amt}; code 1=settled live reservation,
# 2=settled after TTL expiry (spend still committed), 0=unknown/already settled (no-op),
# -1=bad args, -2=corrupt store.
_SETTLE_LUA = (
    _LUA_PRELUDE
    + """
local res_id = ARGV[1]
local actual = tonumber(ARGV[2])
local now = tonumber(ARGV[3])
local window = tonumber(ARGV[4])
if actual == nil or actual < 0 or now == nil or window == nil or window <= 0 then
  return {-1, 0, 0, 0}
end
local raw = redis.call('HGET', KEYS[3], res_id)
if not raw then
  return {0, 0, 0, 0}
end
local tombstone = string.sub(raw, 1, 1) == 'x'
local amt
if tombstone then amt = tonumber(string.sub(raw, 2)) else amt = tonumber(raw) end
local committed, reserved, window_start = load_window()
if amt == nil or committed == nil then return {-2, 0, 0, 0} end

if window_start == 0 or (now - window_start) >= window then
  window_start = now
  committed = 0
end
redis.call('HDEL', KEYS[3], res_id)
redis.call('ZREM', KEYS[2], res_id)
if not tombstone then
  reserved = reserved - amt
  if reserved < 0 then reserved = 0 end
end
committed = committed + actual
redis.call('HSET', KEYS[1], 'window_start', string.format('%d', window_start),
  'committed_micros', string.format('%d', committed),
  'reserved_micros', string.format('%d', reserved))
local code = 1
if tombstone then code = 2 end
return {code, committed, reserved, amt}
"""
)

# ARGV: now_ms. Returns {expired_count, released, committed, reserved} or {-2,...}.
_PURGE_LUA = (
    _LUA_PRELUDE
    + """
local now = tonumber(ARGV[1])
if now == nil then return {-1, 0, 0, 0} end
local committed, reserved, window_start = load_window()
if committed == nil then return {-2, 0, 0, 0} end
local expired_count, released
reserved, expired_count, released = expire(now, reserved)
if reserved == nil then return {-2, 0, 0, 0} end
if expired_count > 0 then
  redis.call('HSET', KEYS[1], 'reserved_micros', string.format('%d', reserved))
end
return {expired_count, released, committed, reserved}
"""
)

MICROS = 1_000_000


def usd_to_micros(usd: float, *, rounding: str = ROUND_HALF_EVEN) -> int:
    """Convert USD to integer micro-units via Decimal (no binary float drift)."""
    value = Decimal(str(usd))
    if not value.is_finite():
        raise ValueError(f"non-finite USD amount: {usd!r}")
    return int((value * MICROS).to_integral_value(rounding=rounding))


def charge_to_micros(usd: float) -> int:
    """Charges (estimates, actual spend) round up so spend is never under-counted."""
    return usd_to_micros(usd, rounding=ROUND_CEILING)


def budget_to_micros(usd: float) -> int:
    """Budget caps round down so the cap is never exceeded by rounding."""
    return usd_to_micros(usd, rounding=ROUND_FLOOR)


def micros_to_usd(micros: int) -> float:
    return int(micros) / MICROS


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    return float(raw)


@dataclass(frozen=True)
class BudgetDecision:
    decision: Decision
    reason: str
    scope: str
    estimated_usd: float
    reserved_usd: float
    committed_usd: float
    budget_usd: float
    soft_warning: bool = False
    reservation_id: str | None = None
    policy_version: str = ""
    policy_sha256: str = ""
    status_code: int = 200


@dataclass
class BudgetReservation:
    guard: InferenceBudgetGuard
    scope: str
    reservation_id: str
    reserved_micros: int
    budget_usd: float
    soft_warning: bool
    policy_version: str
    policy_sha256: str
    window_ms: int = 0
    executed: bool = False
    _settled: bool = False

    @property
    def reserved_usd(self) -> float:
        return micros_to_usd(self.reserved_micros)

    @property
    def settled(self) -> bool:
        return self._settled

    def mark_executed(self) -> None:
        """Record that the upstream model was (or may have been) called."""
        self.executed = True

    async def settle(self, actual_usd: float | None) -> None:
        """Settle once. Unknown or invalid actual spend is charged at the estimate."""
        if self._settled:
            return
        self._settled = True
        actual_micros = self.reserved_micros
        if actual_usd is not None:
            try:
                value = float(actual_usd)
                if math.isfinite(value) and value >= 0:
                    actual_micros = charge_to_micros(value)
            except (TypeError, ValueError):
                pass
        await self.guard._settle(self, actual_micros)

    async def release(self) -> None:
        """Refund the full reservation: the upstream model was never called."""
        if self._settled:
            return
        self._settled = True
        await self.guard._settle(self, 0)

    async def abandon(self) -> None:
        """Error path: refund if nothing executed, else charge the estimate."""
        if self.executed:
            await self.settle(None)
        else:
            await self.release()


def _deny(
    *,
    reason: str,
    status_code: int,
    scope: str = "",
    estimated_usd: float = 0.0,
    reserved_usd: float = 0.0,
    committed_usd: float = 0.0,
    budget_usd: float = 0.0,
    policy_version: str = "",
    policy_sha256: str = "",
) -> BudgetDecision:
    BUDGET_DECISIONS.labels(decision="DENY", reason=reason).inc()
    return BudgetDecision(
        decision="DENY",
        reason=reason,
        scope=scope,
        estimated_usd=estimated_usd,
        reserved_usd=reserved_usd,
        committed_usd=committed_usd,
        budget_usd=budget_usd,
        policy_version=policy_version,
        policy_sha256=policy_sha256,
        status_code=status_code,
    )


def decision_to_http_exception(decision: BudgetDecision) -> HTTPException:
    code = {
        "budget_exceeded": "inference_budget_exceeded",
        "store_unavailable": "budget_store_unavailable",
        "store_timeout": "budget_store_timeout",
        "store_corrupt": "budget_store_corrupt",
        "malformed_policy": "budget_policy_invalid",
        "guard_disabled_misconfigured": "budget_policy_invalid",
    }.get(decision.reason, "inference_budget_denied")
    error_type = "rate_limit_error" if decision.status_code == 429 else "api_error"
    return HTTPException(
        status_code=decision.status_code,
        detail={
            "error": {
                "type": error_type,
                "code": code,
                "message": _message_for(decision),
                "reason": decision.reason,
                "scope": decision.scope or None,
                "budget_usd": decision.budget_usd,
                "committed_usd": decision.committed_usd,
                "reserved_usd": decision.reserved_usd,
                "estimated_usd": decision.estimated_usd,
            }
        },
        headers={
            "Retry-After": "1",
            "X-AI-Budget-Decision": "DENY",
            "X-AI-Budget-Reason": decision.reason,
        },
    )


def _message_for(decision: BudgetDecision) -> str:
    if decision.reason == "budget_exceeded":
        return "inference budget would be exceeded by this reservation"
    if decision.reason == "store_unavailable":
        return "inference budget store unavailable; fail-closed"
    if decision.reason == "store_timeout":
        return "inference budget store timed out; fail-closed"
    if decision.reason == "store_corrupt":
        return "inference budget store returned corrupt data; fail-closed"
    if decision.reason == "malformed_policy":
        return "inference budget policy is malformed; fail-closed"
    return f"inference budget denied: {decision.reason}"


@dataclass
class _MemoryWindow:
    window_start_ms: int = 0
    committed_micros: int = 0
    reserved_micros: int = 0
    # res_id -> (expiry_ms, micros)
    reservations: dict[str, tuple[int, int]] = field(default_factory=dict)
    # Expired-but-unsettled reservations: res_id -> (forget_after_ms, micros)
    tombstones: dict[str, tuple[int, int]] = field(default_factory=dict)


_STORE_TIMEOUT_ERRORS: tuple[type[BaseException], ...] = (TimeoutError, asyncio.TimeoutError)
try:  # redis-py raises its own TimeoutError subclass of RedisError
    from redis.exceptions import TimeoutError as _RedisTimeoutError

    _STORE_TIMEOUT_ERRORS = (*_STORE_TIMEOUT_ERRORS, _RedisTimeoutError)
except ImportError:  # pragma: no cover - redis is a hard dependency
    pass


class InferenceBudgetGuard:
    """Admit inference spend under a per-tenant / per-key window budget."""

    def __init__(
        self,
        policy: InferenceBudgetPolicy | None = None,
        *,
        redis: Redis | None = None,
        store_timeout_seconds: float | None = None,
        clock_ms: Any | None = None,
    ) -> None:
        self._policy = policy
        self._redis = redis
        self._store_timeout_seconds = (
            store_timeout_seconds
            if store_timeout_seconds is not None
            else _env_float("INFERENCE_BUDGET_STORE_TIMEOUT_SECONDS", 1.0)
        )
        self._clock_ms = clock_ms or (lambda: int(time.time() * 1000))
        self._memory: dict[str, _MemoryWindow] = {}
        self._lock = asyncio.Lock()
        self._reserve_sha: str | None = None
        self._settle_sha: str | None = None
        self._purge_sha: str | None = None
        self._redis_ok = redis is not None
        self._last_error: str | None = None
        self._force_unavailable = False
        self._force_timeout = False
        self._force_corrupt = False

    @property
    def enabled(self) -> bool:
        return self._policy is not None and self._policy.enabled

    @property
    def policy(self) -> InferenceBudgetPolicy | None:
        return self._policy

    def status(self) -> dict[str, Any]:
        backend = "redis" if self._redis is not None else "memory"
        return {
            "enabled": self.enabled,
            "backend": backend,
            "redis_configured": self._redis is not None,
            "redis_ok": self._redis_ok if self._redis is not None else None,
            "store_timeout_seconds": self._store_timeout_seconds,
            "policy_version": self._policy.version if self._policy else None,
            "policy_sha256": self._policy.sha256() if self._policy else None,
            "last_error": self._last_error,
        }

    # Test hooks -----------------------------------------------------------------
    def simulate_store_unavailable(self, enabled: bool = True) -> None:
        self._force_unavailable = enabled

    def simulate_store_timeout(self, enabled: bool = True) -> None:
        self._force_timeout = enabled

    def simulate_store_corrupt(self, enabled: bool = True) -> None:
        self._force_corrupt = enabled

    def _keys(self, scope: str) -> list[str]:
        return [
            f"arp:budget:{scope}:window",
            f"arp:budget:{scope}:reservations",
            f"arp:budget:{scope}:amounts",
        ]

    async def _eval_unbounded(
        self, sha_attr: str, script: str, keys: list[str], args: list[object]
    ) -> object:
        assert self._redis is not None
        sha = getattr(self, sha_attr)
        if sha is None:
            sha = await self._redis.script_load(script)
            setattr(self, sha_attr, sha)
        try:
            return await self._redis.evalsha(sha, len(keys), *keys, *args)
        except Exception as error:
            if "NOSCRIPT" not in str(error).upper():
                raise
        sha = await self._redis.script_load(script)
        setattr(self, sha_attr, sha)
        return await self._redis.evalsha(sha, len(keys), *keys, *args)

    async def _eval(
        self, sha_attr: str, script: str, keys: list[str], args: list[object]
    ) -> object:
        """Run a Lua script; the whole exchange (load + eval + reload) is time-bounded."""
        assert self._redis is not None
        if self._force_unavailable:
            raise ConnectionError("simulated budget store unavailable")
        if self._force_timeout:
            raise TimeoutError("simulated budget store timeout")
        if self._force_corrupt:
            # Simulate a corrupt payload without touching the store.
            return ["corrupt"]
        try:
            result = await asyncio.wait_for(
                self._eval_unbounded(sha_attr, script, keys, args),
                timeout=self._store_timeout_seconds,
            )
        except _STORE_TIMEOUT_ERRORS:
            self._redis_ok = False
            self._last_error = "timeout"
            raise TimeoutError("budget store timeout") from None
        except Exception as error:
            self._redis_ok = False
            self._last_error = str(error)
            raise
        self._redis_ok = True
        self._last_error = None
        return result

    def _expire_memory(self, state: _MemoryWindow, now_ms: int, keys_ttl_ms: int) -> int:
        expired_ids = [
            res_id for res_id, (expiry, _amt) in state.reservations.items() if expiry <= now_ms
        ]
        for res_id in expired_ids:
            _expiry, amt = state.reservations.pop(res_id)
            state.reserved_micros = max(0, state.reserved_micros - amt)
            state.tombstones[res_id] = (now_ms + keys_ttl_ms, amt)
        stale = [rid for rid, (forget, _a) in state.tombstones.items() if forget <= now_ms]
        for rid in stale:
            del state.tombstones[rid]
        return len(expired_ids)

    @staticmethod
    def _roll_window(state: _MemoryWindow, now_ms: int, window_ms: int) -> None:
        if state.window_start_ms == 0 or (now_ms - state.window_start_ms) >= window_ms:
            state.window_start_ms = now_ms
            state.committed_micros = 0

    def _deny_for(
        self,
        reason: str,
        status_code: int,
        scope: str,
        scope_budget: ScopeBudget,
        estimate_micros: int,
        policy: InferenceBudgetPolicy,
        *,
        reserved: int = 0,
        committed: int = 0,
    ) -> BudgetDecision:
        return _deny(
            reason=reason,
            status_code=status_code,
            scope=scope,
            estimated_usd=micros_to_usd(estimate_micros),
            reserved_usd=micros_to_usd(reserved),
            committed_usd=micros_to_usd(committed),
            budget_usd=scope_budget.budget_usd,
            policy_version=policy.version,
            policy_sha256=policy.sha256(),
        )

    async def reserve(
        self,
        *,
        tenant_id: str,
        model: str,
        input_tokens: int,
        output_tokens: int,
        api_key: str | None = None,
        estimated_usd: float | None = None,
    ) -> tuple[BudgetDecision, BudgetReservation | None]:
        if self._policy is None:
            # Guard not configured → no-op allow (feature off).
            return (
                BudgetDecision(
                    decision="ALLOW",
                    reason="guard_disabled",
                    scope="",
                    estimated_usd=0.0,
                    reserved_usd=0.0,
                    committed_usd=0.0,
                    budget_usd=0.0,
                ),
                None,
            )

        policy = self._policy
        scope, scope_budget = policy.scope_for(tenant_id, api_key)
        started = time.perf_counter()
        backend = "redis" if self._redis is not None else "memory"
        estimate_micros = 0
        try:
            if estimated_usd is None:
                estimate_micros = policy.estimate_cost_micros(
                    model, input_tokens=input_tokens, output_tokens=output_tokens
                )
            else:
                estimate_micros = charge_to_micros(estimated_usd)
            if estimate_micros < 0:
                raise ValueError("negative estimate")
            if self._redis is not None:
                decision, reservation = await self._reserve_redis(
                    scope, scope_budget, estimate_micros, policy
                )
            else:
                decision, reservation = await self._reserve_memory(
                    scope, scope_budget, estimate_micros, policy
                )
        except _STORE_TIMEOUT_ERRORS:
            BUDGET_RESERVE_LATENCY.labels(backend=backend).observe(time.perf_counter() - started)
            return self._deny_for(
                "store_timeout", 503, scope, scope_budget, estimate_micros, policy
            ), None
        except (TypeError, ValueError, ArithmeticError) as error:
            self._last_error = str(error)
            BUDGET_RESERVE_LATENCY.labels(backend=backend).observe(time.perf_counter() - started)
            return self._deny_for(
                "store_corrupt", 503, scope, scope_budget, estimate_micros, policy
            ), None
        except Exception as error:  # noqa: BLE001 - fail closed on any store fault
            self._last_error = str(error)
            BUDGET_RESERVE_LATENCY.labels(backend=backend).observe(time.perf_counter() - started)
            return self._deny_for(
                "store_unavailable", 503, scope, scope_budget, estimate_micros, policy
            ), None

        BUDGET_RESERVE_LATENCY.labels(backend=backend).observe(time.perf_counter() - started)
        if decision.decision == "ALLOW":
            BUDGET_DECISIONS.labels(decision="ALLOW", reason=decision.reason).inc()
            if decision.soft_warning:
                BUDGET_SOFT_WARNINGS.labels(scope=scope).inc()
            BUDGET_RESERVATIONS.labels(scope=scope).set(decision.reserved_usd)
            BUDGET_COMMITTED.labels(scope=scope).set(decision.committed_usd)
        return decision, reservation

    async def _reserve_redis(
        self,
        scope: str,
        scope_budget: ScopeBudget,
        estimate_micros: int,
        policy: InferenceBudgetPolicy,
    ) -> tuple[BudgetDecision, BudgetReservation | None]:
        res_id = str(uuid.uuid4())
        now_ms = int(self._clock_ms())
        result = await self._eval(
            "_reserve_sha",
            _RESERVE_LUA,
            self._keys(scope),
            [
                now_ms,
                int(scope_budget.window_seconds * 1000),
                budget_to_micros(scope_budget.budget_usd),
                estimate_micros,
                res_id,
                max(1, int(scope_budget.reservation_ttl_seconds * 1000)),
            ],
        )
        return self._interpret_reserve_result(
            result, scope, scope_budget, estimate_micros, policy, res_id
        )

    async def _reserve_memory(
        self,
        scope: str,
        scope_budget: ScopeBudget,
        estimate_micros: int,
        policy: InferenceBudgetPolicy,
    ) -> tuple[BudgetDecision, BudgetReservation | None]:
        if self._force_unavailable:
            raise ConnectionError("simulated budget store unavailable")
        if self._force_timeout:
            raise TimeoutError("simulated budget store timeout")
        if self._force_corrupt:
            return self._interpret_reserve_result(
                ["corrupt"], scope, scope_budget, estimate_micros, policy, ""
            )
        res_id = str(uuid.uuid4())
        now_ms = int(self._clock_ms())
        window_ms = int(scope_budget.window_seconds * 1000)
        ttl_ms = max(1, int(scope_budget.reservation_ttl_seconds * 1000))
        async with self._lock:
            state = self._memory.setdefault(scope, _MemoryWindow())
            self._expire_memory(state, now_ms, max(window_ms, ttl_ms) * 4)
            self._roll_window(state, now_ms, window_ms)
            budget_micros = budget_to_micros(scope_budget.budget_usd)
            if state.committed_micros + state.reserved_micros + estimate_micros > budget_micros:
                result = [0, state.committed_micros, state.reserved_micros]
            else:
                state.reserved_micros += estimate_micros
                state.reservations[res_id] = (now_ms + ttl_ms, estimate_micros)
                result = [1, state.committed_micros, state.reserved_micros]
        return self._interpret_reserve_result(
            result, scope, scope_budget, estimate_micros, policy, res_id
        )

    def _interpret_reserve_result(
        self,
        result: object,
        scope: str,
        scope_budget: ScopeBudget,
        estimate_micros: int,
        policy: InferenceBudgetPolicy,
        res_id: str,
    ) -> tuple[BudgetDecision, BudgetReservation | None]:
        try:
            code = int(result[0])  # type: ignore[index]
            committed = int(result[1])  # type: ignore[index]
            reserved = int(result[2])  # type: ignore[index]
        except (TypeError, ValueError, IndexError, KeyError):
            return self._deny_for(
                "store_corrupt", 503, scope, scope_budget, estimate_micros, policy
            ), None
        if code not in (0, 1) or committed < 0 or reserved < 0:
            return self._deny_for(
                "store_corrupt", 503, scope, scope_budget, estimate_micros, policy
            ), None
        if code == 0:
            return self._deny_for(
                "budget_exceeded",
                429,
                scope,
                scope_budget,
                estimate_micros,
                policy,
                reserved=reserved,
                committed=committed,
            ), None
        budget_micros = budget_to_micros(scope_budget.budget_usd)
        soft_threshold = Decimal(budget_micros) * Decimal(str(scope_budget.soft_warning_ratio))
        soft = Decimal(committed + reserved) >= soft_threshold
        decision = BudgetDecision(
            decision="ALLOW",
            reason="soft_warning" if soft else "within_budget",
            scope=scope,
            estimated_usd=micros_to_usd(estimate_micros),
            reserved_usd=micros_to_usd(reserved),
            committed_usd=micros_to_usd(committed),
            budget_usd=scope_budget.budget_usd,
            soft_warning=soft,
            reservation_id=res_id,
            policy_version=policy.version,
            policy_sha256=policy.sha256(),
        )
        reservation = BudgetReservation(
            guard=self,
            scope=scope,
            reservation_id=res_id,
            reserved_micros=estimate_micros,
            budget_usd=scope_budget.budget_usd,
            soft_warning=soft,
            policy_version=policy.version,
            policy_sha256=policy.sha256(),
            window_ms=int(scope_budget.window_seconds * 1000),
        )
        return decision, reservation

    async def _settle(self, reservation: BudgetReservation, actual_micros: int) -> None:
        actual_micros = max(0, int(actual_micros))
        now_ms = int(self._clock_ms())
        window_ms = reservation.window_ms or 3_600_000
        if self._redis is not None:
            try:
                await self._eval(
                    "_settle_sha",
                    _SETTLE_LUA,
                    self._keys(reservation.scope),
                    [reservation.reservation_id, actual_micros, now_ms, window_ms],
                )
            except Exception as error:  # noqa: BLE001 - settlement lag is a documented limit
                # The reservation stays held until its TTL; this only ever over-counts.
                self._last_error = str(error)
                self._redis_ok = False
            return
        async with self._lock:
            state = self._memory.get(reservation.scope)
            if state is None:
                return
            entry = state.reservations.pop(reservation.reservation_id, None)
            if entry is not None:
                _expiry, reserved_amt = entry
                state.reserved_micros = max(0, state.reserved_micros - reserved_amt)
            elif state.tombstones.pop(reservation.reservation_id, None) is None:
                return  # unknown or already settled: never double-count
            self._roll_window(state, now_ms, window_ms)
            state.committed_micros += actual_micros

    async def purge_expired(self, scope: str) -> int:
        now_ms = int(self._clock_ms())
        if self._redis is not None:
            result = await self._eval("_purge_sha", _PURGE_LUA, self._keys(scope), [now_ms])
            code = int(result[0])  # type: ignore[index]
            if code < 0:
                raise ValueError("corrupt budget store")
            return code
        async with self._lock:
            state = self._memory.get(scope)
            if state is None:
                return 0
            return self._expire_memory(state, now_ms, 4 * 3_600_000)

    async def snapshot_micros(self, scope: str) -> tuple[int, int]:
        """Return (committed_micros, reserved_micros) after expiring stale reservations."""
        now_ms = int(self._clock_ms())
        if self._redis is not None:
            result = await self._eval("_purge_sha", _PURGE_LUA, self._keys(scope), [now_ms])
            if int(result[0]) < 0:  # type: ignore[index]
                raise ValueError("corrupt budget store")
            return int(result[2]), int(result[3])  # type: ignore[index]
        async with self._lock:
            state = self._memory.get(scope)
            if state is None:
                return 0, 0
            self._expire_memory(state, now_ms, 4 * 3_600_000)
            return state.committed_micros, state.reserved_micros

    async def snapshot(self, scope: str) -> tuple[float, float]:
        """Return (committed_usd, reserved_usd) for tests and status."""
        committed, reserved = await self.snapshot_micros(scope)
        return micros_to_usd(committed), micros_to_usd(reserved)

    async def aclose(self) -> None:
        if self._redis is not None:
            await self._redis.aclose()


def create_budget_guard(
    policy: InferenceBudgetPolicy | None | object = ...,
    *,
    redis_url: str | None = None,
    redis: Redis | None = None,
) -> InferenceBudgetGuard | None:
    """Build a guard from env/policy.

    Returns None when the feature is disabled.
    Raises BudgetPolicyError when enabled but misconfigured (fail closed at startup
    wiring — chat path also catches and denies).
    """
    if policy is ...:
        policy = load_inference_budget_policy()
    if policy is None:
        return None
    if isinstance(policy, BudgetPolicyError):
        raise policy
    client = redis
    if client is None and redis_url:
        from redis.asyncio import Redis

        client = Redis.from_url(redis_url, decode_responses=True)
    return InferenceBudgetGuard(policy, redis=client)
