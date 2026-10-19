"""Inference budget guard: atomic reserve → execute → settle on Redis or memory.

Fits the gateway admission path beside fleet concurrency leases. Concurrent
reservations never overspend the configured window budget (on this store).
Store outages, timeouts, and corrupt data fail closed (deny).
"""

from __future__ import annotations

import asyncio
import os
import time
import uuid
from dataclasses import dataclass, field
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

# KEYS[1]=window hash, KEYS[2]=reservations zset, KEYS[3]=reservation amounts hash
# ARGV: now_ms, window_ms, budget_micros, estimate_micros, res_id, ttl_ms
_RESERVE_LUA = """
local now = tonumber(ARGV[1])
local window = tonumber(ARGV[2])
local budget = tonumber(ARGV[3])
local estimate = tonumber(ARGV[4])
local res_id = ARGV[5]
local ttl = tonumber(ARGV[6])

if budget == nil or estimate == nil or now == nil or window == nil or ttl == nil then
  return {-1, 0, 0, 0}
end
if estimate < 0 or budget <= 0 then
  return {-1, 0, 0, 0}
end

local expired = redis.call('ZRANGEBYSCORE', KEYS[2], '-inf', now)
local expired_count = #expired
for i = 1, expired_count do
  local id = expired[i]
  local amt = tonumber(redis.call('HGET', KEYS[3], id) or '0')
  if amt > 0 then
    redis.call('HINCRBY', KEYS[1], 'reserved_micros', -amt)
  end
  redis.call('HDEL', KEYS[3], id)
end
if expired_count > 0 then
  redis.call('ZREMRANGEBYSCORE', KEYS[2], '-inf', now)
end

local window_start = tonumber(redis.call('HGET', KEYS[1], 'window_start') or '0')
if window_start == 0 or (now - window_start) >= window then
  redis.call('HSET', KEYS[1], 'window_start', now, 'committed_micros', 0, 'reserved_micros', 0)
  redis.call('DEL', KEYS[2], KEYS[3])
end

local committed = tonumber(redis.call('HGET', KEYS[1], 'committed_micros') or '0')
local reserved = tonumber(redis.call('HGET', KEYS[1], 'reserved_micros') or '0')
if reserved < 0 then
  reserved = 0
  redis.call('HSET', KEYS[1], 'reserved_micros', 0)
end

if (committed + reserved + estimate) > budget then
  return {0, committed, reserved, committed + reserved, expired_count}
end

redis.call('HINCRBY', KEYS[1], 'reserved_micros', estimate)
redis.call('ZADD', KEYS[2], now + ttl, res_id)
redis.call('HSET', KEYS[3], res_id, tostring(estimate))
redis.call('PEXPIRE', KEYS[1], window * 4)
redis.call('PEXPIRE', KEYS[2], window * 4)
redis.call('PEXPIRE', KEYS[3], window * 4)

local new_reserved = reserved + estimate
return {1, committed, new_reserved, committed + new_reserved, expired_count}
"""

# KEYS[1]=window hash, KEYS[2]=reservations zset, KEYS[3]=reservation amounts hash
# ARGV: res_id, actual_micros
_SETTLE_LUA = """
local res_id = ARGV[1]
local actual = tonumber(ARGV[2])
if actual == nil or actual < 0 then
  return {-1, 0, 0}
end
local reserved_amt = redis.call('HGET', KEYS[3], res_id)
if not reserved_amt then
  return {0, 0, 0}
end
reserved_amt = tonumber(reserved_amt)
redis.call('HDEL', KEYS[3], res_id)
redis.call('ZREM', KEYS[2], res_id)
redis.call('HINCRBY', KEYS[1], 'reserved_micros', -reserved_amt)
redis.call('HINCRBY', KEYS[1], 'committed_micros', actual)
local reserved = tonumber(redis.call('HGET', KEYS[1], 'reserved_micros') or '0')
if reserved < 0 then
  redis.call('HSET', KEYS[1], 'reserved_micros', 0)
  reserved = 0
end
local committed = tonumber(redis.call('HGET', KEYS[1], 'committed_micros') or '0')
return {1, committed, reserved, reserved_amt}
"""

# KEYS[1]=window hash, KEYS[2]=reservations zset, KEYS[3]=reservation amounts hash
# ARGV: now_ms
_PURGE_LUA = """
local now = tonumber(ARGV[1])
local expired = redis.call('ZRANGEBYSCORE', KEYS[2], '-inf', now)
local expired_count = #expired
local released = 0
for i = 1, expired_count do
  local id = expired[i]
  local amt = tonumber(redis.call('HGET', KEYS[3], id) or '0')
  if amt > 0 then
    redis.call('HINCRBY', KEYS[1], 'reserved_micros', -amt)
    released = released + amt
  end
  redis.call('HDEL', KEYS[3], id)
end
if expired_count > 0 then
  redis.call('ZREMRANGEBYSCORE', KEYS[2], '-inf', now)
end
local reserved = tonumber(redis.call('HGET', KEYS[1], 'reserved_micros') or '0')
if reserved < 0 then
  redis.call('HSET', KEYS[1], 'reserved_micros', 0)
  reserved = 0
end
local committed = tonumber(redis.call('HGET', KEYS[1], 'committed_micros') or '0')
return {expired_count, released, committed, reserved}
"""

MICROS = 1_000_000


def usd_to_micros(usd: float) -> int:
    return int(round(float(usd) * MICROS))


def micros_to_usd(micros: int) -> float:
    return round(int(micros) / MICROS, 8)


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
    _settled: bool = False

    @property
    def reserved_usd(self) -> float:
        return micros_to_usd(self.reserved_micros)

    async def settle(self, actual_usd: float) -> None:
        if self._settled:
            return
        self._settled = True
        await self.guard._settle(self, usd_to_micros(actual_usd))

    async def release(self) -> None:
        """Refund the full reservation (unused / failed request)."""
        await self.settle(0.0)


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
    reservations: dict[str, tuple[int, int]] = field(default_factory=dict)


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

    def _window_key(self, scope: str) -> str:
        return f"arp:budget:{scope}:window"

    def _reservations_key(self, scope: str) -> str:
        return f"arp:budget:{scope}:reservations"

    def _amounts_key(self, scope: str) -> str:
        return f"arp:budget:{scope}:amounts"

    async def _ensure_scripts(self) -> None:
        assert self._redis is not None
        if self._reserve_sha is None:
            self._reserve_sha = await self._redis.script_load(_RESERVE_LUA)
        if self._settle_sha is None:
            self._settle_sha = await self._redis.script_load(_SETTLE_LUA)
        if self._purge_sha is None:
            self._purge_sha = await self._redis.script_load(_PURGE_LUA)

    async def _eval(
        self, sha_attr: str, script: str, keys: list[str], args: list[object]
    ) -> object:
        assert self._redis is not None
        if self._force_unavailable:
            raise ConnectionError("simulated budget store unavailable")
        if self._force_timeout:
            raise TimeoutError("simulated budget store timeout")
        await self._ensure_scripts()
        sha = getattr(self, sha_attr)
        try:
            result = await asyncio.wait_for(
                self._redis.evalsha(sha, len(keys), *keys, *args),
                timeout=self._store_timeout_seconds,
            )
        except TimeoutError:
            self._redis_ok = False
            self._last_error = "timeout"
            raise
        except Exception as error:  # noqa: BLE001 - NOSCRIPT reload path
            if "NOSCRIPT" not in str(error).upper():
                self._redis_ok = False
                self._last_error = str(error)
                raise
            sha = await self._redis.script_load(script)
            setattr(self, sha_attr, sha)
            result = await asyncio.wait_for(
                self._redis.evalsha(sha, len(keys), *keys, *args),
                timeout=self._store_timeout_seconds,
            )
        if self._force_corrupt:
            return ["corrupt"]
        self._redis_ok = True
        self._last_error = None
        return result

    def _purge_memory(self, state: _MemoryWindow, now_ms: int) -> int:
        expired_ids = [
            res_id
            for res_id, (expiry, _amt) in list(state.reservations.items())
            if expiry <= now_ms
        ]
        released = 0
        for res_id in expired_ids:
            _expiry, amt = state.reservations.pop(res_id)
            state.reserved_micros = max(0, state.reserved_micros - amt)
            released += amt
        return len(expired_ids)

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
        policy_sha = policy.sha256()
        scope, scope_budget = policy.scope_for(tenant_id, api_key)
        if estimated_usd is None:
            estimated_usd = policy.estimate_cost_usd(
                model, input_tokens=input_tokens, output_tokens=output_tokens
            )
        estimate_micros = usd_to_micros(estimated_usd)
        started = time.perf_counter()
        backend = "redis" if self._redis is not None else "memory"

        try:
            if self._redis is not None:
                decision, reservation = await self._reserve_redis(
                    scope, scope_budget, estimate_micros, policy
                )
            else:
                decision, reservation = await self._reserve_memory(
                    scope, scope_budget, estimate_micros, policy
                )
        except TimeoutError:
            BUDGET_RESERVE_LATENCY.labels(backend=backend).observe(time.perf_counter() - started)
            return (
                _deny(
                    reason="store_timeout",
                    status_code=503,
                    scope=scope,
                    estimated_usd=estimated_usd,
                    budget_usd=scope_budget.budget_usd,
                    policy_version=policy.version,
                    policy_sha256=policy_sha,
                ),
                None,
            )
        except Exception as error:  # noqa: BLE001 - fail closed on any store fault
            self._last_error = str(error)
            BUDGET_RESERVE_LATENCY.labels(backend=backend).observe(time.perf_counter() - started)
            return (
                _deny(
                    reason="store_unavailable",
                    status_code=503,
                    scope=scope,
                    estimated_usd=estimated_usd,
                    budget_usd=scope_budget.budget_usd,
                    policy_version=policy.version,
                    policy_sha256=policy_sha,
                ),
                None,
            )

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
            [self._window_key(scope), self._reservations_key(scope), self._amounts_key(scope)],
            [
                now_ms,
                int(scope_budget.window_seconds * 1000),
                usd_to_micros(scope_budget.budget_usd),
                estimate_micros,
                res_id,
                int(scope_budget.reservation_ttl_seconds * 1000),
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
        res_id = str(uuid.uuid4())
        now_ms = int(self._clock_ms())
        async with self._lock:
            if self._force_corrupt:
                return (
                    _deny(
                        reason="store_corrupt",
                        status_code=503,
                        scope=scope,
                        estimated_usd=micros_to_usd(estimate_micros),
                        budget_usd=scope_budget.budget_usd,
                        policy_version=policy.version,
                        policy_sha256=policy.sha256(),
                    ),
                    None,
                )
            state = self._memory.setdefault(scope, _MemoryWindow())
            self._purge_memory(state, now_ms)
            window_ms = int(scope_budget.window_seconds * 1000)
            if state.window_start_ms == 0 or (now_ms - state.window_start_ms) >= window_ms:
                state.window_start_ms = now_ms
                state.committed_micros = 0
                state.reserved_micros = 0
                state.reservations.clear()
            budget_micros = usd_to_micros(scope_budget.budget_usd)
            total = state.committed_micros + state.reserved_micros + estimate_micros
            if total > budget_micros:
                return (
                    _deny(
                        reason="budget_exceeded",
                        status_code=429,
                        scope=scope,
                        estimated_usd=micros_to_usd(estimate_micros),
                        reserved_usd=micros_to_usd(state.reserved_micros),
                        committed_usd=micros_to_usd(state.committed_micros),
                        budget_usd=scope_budget.budget_usd,
                        policy_version=policy.version,
                        policy_sha256=policy.sha256(),
                    ),
                    None,
                )
            state.reserved_micros += estimate_micros
            state.reservations[res_id] = (
                now_ms + int(scope_budget.reservation_ttl_seconds * 1000),
                estimate_micros,
            )
            result = [
                1,
                state.committed_micros,
                state.reserved_micros,
                state.committed_micros + state.reserved_micros,
                0,
            ]
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
        except (TypeError, ValueError, IndexError):
            return (
                _deny(
                    reason="store_corrupt",
                    status_code=503,
                    scope=scope,
                    estimated_usd=micros_to_usd(estimate_micros),
                    budget_usd=scope_budget.budget_usd,
                    policy_version=policy.version,
                    policy_sha256=policy.sha256(),
                ),
                None,
            )
        if code < 0:
            return (
                _deny(
                    reason="store_corrupt",
                    status_code=503,
                    scope=scope,
                    estimated_usd=micros_to_usd(estimate_micros),
                    budget_usd=scope_budget.budget_usd,
                    policy_version=policy.version,
                    policy_sha256=policy.sha256(),
                ),
                None,
            )
        if code == 0:
            return (
                _deny(
                    reason="budget_exceeded",
                    status_code=429,
                    scope=scope,
                    estimated_usd=micros_to_usd(estimate_micros),
                    reserved_usd=micros_to_usd(reserved),
                    committed_usd=micros_to_usd(committed),
                    budget_usd=scope_budget.budget_usd,
                    policy_version=policy.version,
                    policy_sha256=policy.sha256(),
                ),
                None,
            )
        total_usd = micros_to_usd(committed + reserved)
        soft = total_usd >= scope_budget.budget_usd * scope_budget.soft_warning_ratio
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
        )
        return decision, reservation

    async def _settle(self, reservation: BudgetReservation, actual_micros: int) -> None:
        if self._redis is not None:
            try:
                await self._eval(
                    "_settle_sha",
                    _SETTLE_LUA,
                    [
                        self._window_key(reservation.scope),
                        self._reservations_key(reservation.scope),
                        self._amounts_key(reservation.scope),
                    ],
                    [reservation.reservation_id, actual_micros],
                )
            except Exception as error:  # noqa: BLE001 - settlement lag is a documented limit
                self._last_error = str(error)
                self._redis_ok = False
            return
        async with self._lock:
            state = self._memory.get(reservation.scope)
            if state is None:
                return
            entry = state.reservations.pop(reservation.reservation_id, None)
            if entry is None:
                return
            _expiry, reserved_amt = entry
            state.reserved_micros = max(0, state.reserved_micros - reserved_amt)
            state.committed_micros += max(0, actual_micros)

    async def purge_expired(self, scope: str) -> int:
        now_ms = int(self._clock_ms())
        if self._redis is not None:
            result = await self._eval(
                "_purge_sha",
                _PURGE_LUA,
                [self._window_key(scope), self._reservations_key(scope), self._amounts_key(scope)],
                [now_ms],
            )
            return int(result[0])  # type: ignore[index]
        async with self._lock:
            state = self._memory.get(scope)
            if state is None:
                return 0
            return self._purge_memory(state, now_ms)

    async def snapshot(self, scope: str) -> tuple[float, float]:
        """Return (committed_usd, reserved_usd) for tests and status."""
        if self._redis is not None:
            now_ms = int(self._clock_ms())
            result = await self._eval(
                "_purge_sha",
                _PURGE_LUA,
                [self._window_key(scope), self._reservations_key(scope), self._amounts_key(scope)],
                [now_ms],
            )
            return micros_to_usd(int(result[2])), micros_to_usd(int(result[3]))  # type: ignore[index]
        async with self._lock:
            state = self._memory.get(scope)
            if state is None:
                return 0.0, 0.0
            self._purge_memory(state, int(self._clock_ms()))
            return micros_to_usd(state.committed_micros), micros_to_usd(state.reserved_micros)

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
