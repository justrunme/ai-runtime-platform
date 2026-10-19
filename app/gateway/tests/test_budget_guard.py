"""Inference budget guard: atomicity, fail-closed, settlement, evidence."""

from __future__ import annotations

import asyncio
import json
import os
import time
from pathlib import Path

import fakeredis.aioredis
import pytest

from app.gateway.budget_evidence import build_budget_envelope
from app.gateway.budget_guard import InferenceBudgetGuard, create_budget_guard, usd_to_micros
from app.gateway.budget_policy import (
    BudgetPolicyError,
    InferenceBudgetPolicy,
    reset_inference_budget_policy_cache,
)

POLICY = InferenceBudgetPolicy.model_validate(
    {
        "version": "budget-guard-1.0",
        "enabled": True,
        "default": {
            "budgetUsd": 1.0,
            "windowSeconds": 3600,
            "softWarningRatio": 0.8,
            "reservationTtlSeconds": 0.3,
        },
        "tenants": {
            "finance": {
                "budgetUsd": 0.10,
                "windowSeconds": 3600,
                "softWarningRatio": 0.8,
                "reservationTtlSeconds": 0.3,
                "apiKeys": {
                    "key-limited": {
                        "budgetUsd": 0.05,
                        "windowSeconds": 3600,
                        "softWarningRatio": 0.9,
                        "reservationTtlSeconds": 0.3,
                    }
                },
            }
        },
        "prices": {
            "qwen": {"inputPerMillion": 1.0, "outputPerMillion": 2.0},
            "*": {"inputPerMillion": 5.0, "outputPerMillion": 5.0},
        },
    }
)

# Collect envelopes for CI evidence generation.
EVIDENCE_ENVELOPES: list[dict] = []
CONCURRENCY_RESULTS: list[dict] = []


def _record(scenario_id: str, decision, **details) -> dict:
    envelope = build_budget_envelope(decision, scenario_id=scenario_id, details=details)
    EVIDENCE_ENVELOPES.append(envelope)
    return envelope


@pytest.fixture(autouse=True)
def _reset_policy_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    reset_inference_budget_policy_cache()
    monkeypatch.delenv("INFERENCE_BUDGET_ENABLED", raising=False)
    monkeypatch.delenv("INFERENCE_BUDGET_POLICY", raising=False)
    monkeypatch.delenv("INFERENCE_BUDGET_POLICY_FILE", raising=False)
    yield
    reset_inference_budget_policy_cache()


@pytest.mark.anyio
async def test_within_budget_allows_and_records_false_deny_zero() -> None:
    guard = InferenceBudgetGuard(POLICY)
    false_denies = 0
    allows = 0
    for i in range(5):
        decision, reservation = await guard.reserve(
            tenant_id="research",
            model="qwen",
            input_tokens=1_000,
            output_tokens=1_000,
        )
        _record("legitimate_within_budget", decision, iteration=i)
        if decision.decision == "DENY":
            false_denies += 1
        else:
            allows += 1
            assert reservation is not None
            await reservation.settle(decision.estimated_usd)
    assert false_denies == 0
    assert allows == 5


@pytest.mark.anyio
async def test_over_budget_request_denied() -> None:
    guard = InferenceBudgetGuard(POLICY)
    # finance budget $0.10; 100k input tokens @ $1/M = $0.10 exactly still fits;
    # 100k + 1 output pushes over when reserved fully.
    decision, reservation = await guard.reserve(
        tenant_id="finance",
        model="qwen",
        input_tokens=100_000,
        output_tokens=50_000,
    )
    _record("over_budget_denied", decision)
    assert decision.decision == "DENY"
    assert decision.reason == "budget_exceeded"
    assert decision.status_code == 429
    assert reservation is None


@pytest.mark.anyio
async def test_settlement_refunds_unused_reservation() -> None:
    guard = InferenceBudgetGuard(POLICY)
    decision, reservation = await guard.reserve(
        tenant_id="finance",
        model="qwen",
        input_tokens=50_000,
        output_tokens=0,
    )
    assert decision.decision == "ALLOW"
    assert reservation is not None
    scope = reservation.scope
    committed, reserved = await guard.snapshot(scope)
    assert reserved == pytest.approx(0.05)
    # Actual usage is cheaper → unused estimate refunded from reserved.
    await reservation.settle(0.01)
    committed, reserved = await guard.snapshot(scope)
    assert reserved == pytest.approx(0.0)
    assert committed == pytest.approx(0.01)
    _record(
        "settlement_refunds_unused",
        decision,
        committed_after=committed,
        reserved_after=reserved,
    )


@pytest.mark.anyio
async def test_expired_reservation_is_released() -> None:
    clock = {"ms": int(time.time() * 1000)}

    def now() -> int:
        return clock["ms"]

    guard = InferenceBudgetGuard(POLICY, clock_ms=now)
    decision, reservation = await guard.reserve(
        tenant_id="finance",
        model="qwen",
        input_tokens=50_000,
        output_tokens=0,
    )
    assert decision.decision == "ALLOW"
    assert reservation is not None
    scope = reservation.scope
    _, reserved_before = await guard.snapshot(scope)
    assert reserved_before == pytest.approx(0.05)
    # Advance past reservation TTL (0.3s).
    clock["ms"] += 500
    purged = await guard.purge_expired(scope)
    assert purged == 1
    _, reserved_after = await guard.snapshot(scope)
    assert reserved_after == pytest.approx(0.0)
    _record(
        "expired_reservation_released",
        decision,
        purged=purged,
        reserved_after=reserved_after,
    )


@pytest.mark.anyio
async def test_store_down_fails_closed() -> None:
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    guard = InferenceBudgetGuard(POLICY, redis=redis)
    guard.simulate_store_unavailable(True)
    decision, reservation = await guard.reserve(
        tenant_id="finance",
        model="qwen",
        input_tokens=1_000,
        output_tokens=0,
    )
    _record("store_down_fail_closed", decision)
    assert decision.decision == "DENY"
    assert decision.reason == "store_unavailable"
    assert decision.status_code == 503
    assert reservation is None


@pytest.mark.anyio
async def test_store_timeout_fails_closed() -> None:
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    guard = InferenceBudgetGuard(POLICY, redis=redis, store_timeout_seconds=0.01)
    guard.simulate_store_timeout(True)
    decision, reservation = await guard.reserve(
        tenant_id="finance",
        model="qwen",
        input_tokens=1_000,
        output_tokens=0,
    )
    _record("store_timeout_fail_closed", decision)
    assert decision.decision == "DENY"
    assert decision.reason == "store_timeout"
    assert decision.status_code == 503
    assert reservation is None


@pytest.mark.anyio
async def test_store_corrupt_fails_closed() -> None:
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    guard = InferenceBudgetGuard(POLICY, redis=redis)
    guard.simulate_store_corrupt(True)
    decision, reservation = await guard.reserve(
        tenant_id="finance",
        model="qwen",
        input_tokens=1_000,
        output_tokens=0,
    )
    _record("store_corrupt_fail_closed", decision)
    assert decision.decision == "DENY"
    assert decision.reason == "store_corrupt"
    assert decision.status_code == 503
    assert reservation is None


def test_malformed_policy_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("INFERENCE_BUDGET_ENABLED", "true")
    monkeypatch.setenv("INFERENCE_BUDGET_POLICY", "{not-json")
    reset_inference_budget_policy_cache()
    with pytest.raises(BudgetPolicyError):
        create_budget_guard()
    # Evidence: synthetic DENY for malformed policy path.
    from app.gateway.budget_guard import BudgetDecision

    decision = BudgetDecision(
        decision="DENY",
        reason="malformed_policy",
        scope="",
        estimated_usd=0.0,
        reserved_usd=0.0,
        committed_usd=0.0,
        budget_usd=0.0,
        status_code=503,
        policy_version="invalid",
        policy_sha256="sha256:" + ("0" * 64),
    )
    _record("malformed_policy_fail_closed", decision)


@pytest.mark.anyio
@pytest.mark.parametrize("backend", ["memory", "fakeredis"])
async def test_concurrency_never_overspends(backend: str) -> None:
    """Single-process smoke: asyncio.gather of reserves (fakeredis serialises calls).

    Real parallelism against a real Redis server is covered by the multiprocess tests.
    """
    budget_usd = 0.10
    # Each reserve costs $0.02 → at most 5 allows.
    estimate = 0.02
    n = 40
    rounds = 25
    max_allows = int(budget_usd // estimate)

    for round_idx in range(rounds):
        if backend == "fakeredis":
            redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
            guard = InferenceBudgetGuard(POLICY, redis=redis)
        else:
            guard = InferenceBudgetGuard(POLICY)

        async def one(_: int, g: InferenceBudgetGuard = guard) -> str:
            decision, reservation = await g.reserve(
                tenant_id="finance",
                model="qwen",
                input_tokens=0,
                output_tokens=0,
                estimated_usd=estimate,
            )
            if decision.decision == "ALLOW":
                assert reservation is not None
                # Hold reservation (do not settle) so concurrent pressure is real.
                return "ALLOW"
            return "DENY"

        outcomes = await asyncio.gather(*[one(i) for i in range(n)])
        allows = outcomes.count("ALLOW")
        committed, reserved = await guard.snapshot("tenant:finance")
        total = committed + reserved
        assert allows <= max_allows
        assert total <= budget_usd + 1e-9
        assert usd_to_micros(total) <= usd_to_micros(budget_usd)
        CONCURRENCY_RESULTS.append(
            {
                "backend": backend,
                "round": round_idx,
                "allows": allows,
                "denies": outcomes.count("DENY"),
                "reserved_usd": reserved,
                "committed_usd": committed,
                "total_usd": total,
                "budget_usd": budget_usd,
                "max_allows": max_allows,
            }
        )
        # One envelope per backend (first round) for the evidence table.
        if round_idx == 0:
            from app.gateway.budget_guard import BudgetDecision

            decision = BudgetDecision(
                decision="ALLOW",
                reason="concurrency_invariant_held",
                scope="tenant:finance",
                estimated_usd=estimate,
                reserved_usd=reserved,
                committed_usd=committed,
                budget_usd=budget_usd,
                policy_version=POLICY.version,
                policy_sha256=POLICY.sha256(),
            )
            _record(
                "concurrency_atomic_redis"
                if backend == "fakeredis"
                else "concurrency_atomic_memory",
                decision,
                rounds=rounds,
                parallel=n,
                max_allows_observed=allows,
            )
        await guard.aclose()


@pytest.mark.anyio
async def test_redis_and_memory_agree_on_hard_cap() -> None:
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    for guard in (InferenceBudgetGuard(POLICY), InferenceBudgetGuard(POLICY, redis=redis)):
        allows = 0
        while True:
            decision, reservation = await guard.reserve(
                tenant_id="finance",
                model="qwen",
                input_tokens=0,
                output_tokens=0,
                estimated_usd=0.03,
            )
            if decision.decision == "DENY":
                break
            allows += 1
            assert reservation is not None
        assert allows == 3  # 0.03 * 3 = 0.09 <= 0.10; fourth would be 0.12
        await guard.aclose()


@pytest.mark.anyio
async def test_per_key_budget_is_isolated() -> None:
    guard = InferenceBudgetGuard(POLICY)
    decision, reservation = await guard.reserve(
        tenant_id="finance",
        model="qwen",
        input_tokens=0,
        output_tokens=0,
        api_key="key-limited",
        estimated_usd=0.04,
    )
    assert decision.decision == "ALLOW"
    assert reservation is not None
    assert reservation.scope.startswith("key:")
    decision2, _ = await guard.reserve(
        tenant_id="finance",
        model="qwen",
        input_tokens=0,
        output_tokens=0,
        api_key="key-limited",
        estimated_usd=0.02,
    )
    assert decision2.decision == "DENY"
    # Tenant-level budget still has capacity independent of key scope.
    decision3, res3 = await guard.reserve(
        tenant_id="finance",
        model="qwen",
        input_tokens=0,
        output_tokens=0,
        estimated_usd=0.05,
    )
    assert decision3.decision == "ALLOW"
    assert res3 is not None
    await reservation.settle(0.04)
    await res3.settle(0.05)


# --------------------------------------------------------------------------------------
# Review hardening: negative tests (money, settle/expiry, rollover, fail-closed, secrets)
# --------------------------------------------------------------------------------------

REAL_REDIS_URL = os.environ.get("BUDGET_GUARD_REDIS_URL", "").strip()
REQUIRE_REAL_REDIS = os.environ.get("BUDGET_GUARD_REQUIRE_REDIS", "").strip() == "1"
BACKENDS = ["memory", "fakeredis"] + (["redis"] if REAL_REDIS_URL else [])


async def _make_guard(backend: str, *, policy=POLICY, clock_ms=None, **kwargs):
    if backend == "memory":
        return InferenceBudgetGuard(policy, clock_ms=clock_ms, **kwargs)
    if backend == "fakeredis":
        client = fakeredis.aioredis.FakeRedis(decode_responses=True)
    else:
        from redis.asyncio import Redis

        client = Redis.from_url(REAL_REDIS_URL, decode_responses=True)
        await client.flushdb()
    return InferenceBudgetGuard(policy, redis=client, clock_ms=clock_ms, **kwargs)


class _Clock:
    def __init__(self) -> None:
        self.ms = int(time.time() * 1000)

    def __call__(self) -> int:
        return self.ms


def test_real_redis_configured_when_required() -> None:
    """CI sets BUDGET_GUARD_REQUIRE_REDIS=1 so real-Redis tests can never silently skip."""
    if REQUIRE_REAL_REDIS:
        assert REAL_REDIS_URL, "BUDGET_GUARD_REDIS_URL must be set when Redis is required"


def test_estimate_is_integer_micros_rounded_up() -> None:
    # 1 input token at $0.10/M = 0.1 micro-USD: must reserve 1 micro, never 0.
    policy = InferenceBudgetPolicy.model_validate(
        {
            "version": "t",
            "default": {"budgetUsd": 1.0},
            "prices": {"*": {"inputPerMillion": 0.1, "outputPerMillion": 0.1}},
        }
    )
    assert policy.estimate_cost_micros("m", input_tokens=1, output_tokens=0) == 1
    assert policy.estimate_cost_micros("m", input_tokens=0, output_tokens=0) == 0
    assert usd_to_micros(0.1) + usd_to_micros(0.2) == usd_to_micros(0.3)


@pytest.mark.anyio
@pytest.mark.parametrize("backend", BACKENDS)
async def test_float_sums_do_not_false_deny(backend: str) -> None:
    # 0.1 + 0.1 + 0.1 > 0.3 in binary floats; integer micros must admit all three.
    policy = InferenceBudgetPolicy.model_validate(
        {
            "version": "t",
            "default": {"budgetUsd": 0.3},
            "prices": {"*": {"inputPerMillion": 1, "outputPerMillion": 1}},
        }
    )
    guard = await _make_guard(backend, policy=policy)
    outcomes = []
    for _ in range(3):
        decision, _res = await guard.reserve(
            tenant_id="t", model="m", input_tokens=0, output_tokens=0, estimated_usd=0.1
        )
        outcomes.append(decision.decision)
    decision, _ = await guard.reserve(
        tenant_id="t", model="m", input_tokens=0, output_tokens=0, estimated_usd=0.000001
    )
    assert outcomes == ["ALLOW", "ALLOW", "ALLOW"]
    assert decision.decision == "DENY"
    await guard.aclose()


@pytest.mark.anyio
@pytest.mark.parametrize("backend", BACKENDS)
async def test_settle_after_ttl_expiry_commits_spend_exactly_once(backend: str) -> None:
    clock = _Clock()
    guard = await _make_guard(backend, clock_ms=clock)
    decision, reservation = await guard.reserve(
        tenant_id="finance", model="qwen", input_tokens=0, output_tokens=0, estimated_usd=0.05
    )
    assert decision.decision == "ALLOW" and reservation is not None
    clock.ms += 1_000  # past the 0.3 s TTL
    assert await guard.purge_expired(reservation.scope) == 1
    assert await guard.snapshot_micros(reservation.scope) == (0, 0)
    # The request did run: its real spend must still be committed...
    await reservation.settle(0.03)
    assert await guard.snapshot_micros(reservation.scope) == (30_000, 0)
    # ...exactly once, even if a second settle for the same id arrives.
    from app.gateway.budget_guard import BudgetReservation

    duplicate = BudgetReservation(**{**reservation.__dict__, "_settled": False})
    await duplicate.settle(0.03)
    await duplicate.release()
    assert await guard.snapshot_micros(reservation.scope) == (30_000, 0)
    await guard.aclose()


@pytest.mark.anyio
@pytest.mark.parametrize("backend", BACKENDS)
async def test_settle_never_refunds_more_than_reserved(backend: str) -> None:
    guard = await _make_guard(backend)
    _, keep = await guard.reserve(
        tenant_id="finance", model="qwen", input_tokens=0, output_tokens=0, estimated_usd=0.04
    )
    _, res = await guard.reserve(
        tenant_id="finance", model="qwen", input_tokens=0, output_tokens=0, estimated_usd=0.02
    )
    assert keep is not None and res is not None
    await res.release()
    await res.release()
    await res.settle(0.0)
    # Other in-flight reservation must remain fully held.
    assert await guard.snapshot_micros(res.scope) == (0, 40_000)
    await keep.settle(0.01)
    assert await guard.snapshot_micros(res.scope) == (10_000, 0)
    await guard.aclose()


@pytest.mark.anyio
@pytest.mark.parametrize("backend", BACKENDS)
async def test_window_rollover_keeps_in_flight_reservations(backend: str) -> None:
    clock = _Clock()
    policy = InferenceBudgetPolicy.model_validate(
        {
            "version": "t",
            "default": {"budgetUsd": 0.10, "windowSeconds": 1, "reservationTtlSeconds": 60},
            "prices": {"*": {"inputPerMillion": 1, "outputPerMillion": 1}},
        }
    )
    guard = await _make_guard(backend, policy=policy, clock_ms=clock)
    _, in_flight = await guard.reserve(
        tenant_id="t", model="m", input_tokens=0, output_tokens=0, estimated_usd=0.08
    )
    assert in_flight is not None
    clock.ms += 1_500  # window rolls over while the request is still running
    decision, _ = await guard.reserve(
        tenant_id="t", model="m", input_tokens=0, output_tokens=0, estimated_usd=0.05
    )
    assert decision.decision == "DENY"  # 0.08 still in flight
    await in_flight.settle(0.08)
    assert await guard.snapshot_micros(in_flight.scope) == (80_000, 0)
    await guard.aclose()


@pytest.mark.anyio
@pytest.mark.parametrize("backend", BACKENDS)
@pytest.mark.parametrize("actual", [None, float("nan"), float("inf"), -1.0, "garbage"])
async def test_unknown_or_invalid_actual_is_charged_at_estimate(backend: str, actual) -> None:
    guard = await _make_guard(backend)
    _, res = await guard.reserve(
        tenant_id="finance", model="qwen", input_tokens=0, output_tokens=0, estimated_usd=0.02
    )
    assert res is not None
    await res.settle(actual)
    assert await guard.snapshot_micros(res.scope) == (20_000, 0)
    await guard.aclose()


@pytest.mark.anyio
async def test_abandon_refunds_before_execution_and_charges_after() -> None:
    guard = InferenceBudgetGuard(POLICY)
    _, before = await guard.reserve(
        tenant_id="finance", model="qwen", input_tokens=0, output_tokens=0, estimated_usd=0.02
    )
    _, after = await guard.reserve(
        tenant_id="finance", model="qwen", input_tokens=0, output_tokens=0, estimated_usd=0.03
    )
    assert before is not None and after is not None
    await before.abandon()
    after.mark_executed()
    await after.abandon()
    assert await guard.snapshot_micros(after.scope) == (30_000, 0)


def test_api_key_is_not_stored_in_scope() -> None:
    scope, _ = POLICY.scope_for("finance", "key-limited")
    assert "key-limited" not in scope
    assert scope.startswith("key:finance:")


@pytest.mark.anyio
@pytest.mark.parametrize("backend", [b for b in BACKENDS if b != "memory"])
async def test_simulated_corrupt_does_not_leave_reservation(backend: str) -> None:
    guard = await _make_guard(backend)
    guard.simulate_store_corrupt(True)
    decision, _ = await guard.reserve(
        tenant_id="finance", model="qwen", input_tokens=0, output_tokens=0, estimated_usd=0.05
    )
    assert decision.reason == "store_corrupt"
    guard.simulate_store_corrupt(False)
    assert await guard.snapshot_micros("tenant:finance") == (0, 0)
    await guard.aclose()


@pytest.mark.anyio
@pytest.mark.parametrize("backend", [b for b in BACKENDS if b != "memory"])
@pytest.mark.parametrize("field", ["committed_micros", "reserved_micros", "window_start"])
async def test_corrupt_stored_window_fails_closed(backend: str, field: str) -> None:
    guard = await _make_guard(backend)
    await guard._redis.hset("arp:budget:tenant:finance:window", field, "not-a-number")
    decision, res = await guard.reserve(
        tenant_id="finance", model="qwen", input_tokens=0, output_tokens=0, estimated_usd=0.01
    )
    assert decision.decision == "DENY"
    assert decision.reason == "store_corrupt"
    assert decision.status_code == 503
    assert res is None
    await guard.aclose()


@pytest.mark.anyio
@pytest.mark.parametrize("backend", [b for b in BACKENDS if b != "memory"])
async def test_corrupt_reservation_amount_fails_closed(backend: str) -> None:
    clock = _Clock()
    guard = await _make_guard(backend, clock_ms=clock)
    _, res = await guard.reserve(
        tenant_id="finance", model="qwen", input_tokens=0, output_tokens=0, estimated_usd=0.01
    )
    assert res is not None
    await guard._redis.hset("arp:budget:tenant:finance:amounts", res.reservation_id, "zzz")
    clock.ms += 1_000
    decision, _ = await guard.reserve(
        tenant_id="finance", model="qwen", input_tokens=0, output_tokens=0, estimated_usd=0.01
    )
    assert decision.reason == "store_corrupt"
    await guard.aclose()


class _HangingRedis:
    """Store whose every call hangs: script load must be time-bounded too."""

    async def script_load(self, script: str) -> str:
        await asyncio.sleep(30)
        return "sha"

    async def evalsha(self, *args):
        await asyncio.sleep(30)

    async def aclose(self) -> None:
        return None


class _RedisPyTimeoutRedis(_HangingRedis):
    async def script_load(self, script: str) -> str:
        from redis.exceptions import TimeoutError as RedisTimeoutError

        raise RedisTimeoutError("Timeout reading from socket")


class _BrokenRedis(_HangingRedis):
    async def script_load(self, script: str) -> str:
        raise RuntimeError("unexpected client bug")


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("client", "reason"),
    [
        (_HangingRedis(), "store_timeout"),
        (_RedisPyTimeoutRedis(), "store_timeout"),
        (_BrokenRedis(), "store_unavailable"),
    ],
)
async def test_every_store_error_path_denies(client, reason: str) -> None:
    guard = InferenceBudgetGuard(POLICY, redis=client, store_timeout_seconds=0.05)  # type: ignore[arg-type]
    started = time.perf_counter()
    decision, res = await guard.reserve(
        tenant_id="finance", model="qwen", input_tokens=10, output_tokens=10
    )
    assert time.perf_counter() - started < 2.0
    assert decision.decision == "DENY"
    assert decision.reason == reason
    assert decision.status_code == 503
    assert res is None


@pytest.mark.anyio
@pytest.mark.parametrize("bad", [float("nan"), float("inf"), -0.5])
async def test_invalid_estimate_fails_closed(bad: float) -> None:
    guard = InferenceBudgetGuard(POLICY)
    decision, res = await guard.reserve(
        tenant_id="finance", model="qwen", input_tokens=0, output_tokens=0, estimated_usd=bad
    )
    assert decision.decision == "DENY" and decision.status_code == 503
    assert res is None


# ----------------------------- gateway chat path ---------------------------------------


def _gateway(handler, guard, *, tenant_store=None):
    import httpx

    from app.gateway.main import (
        BackendHealthStore,
        GatewaySettings,
        ModelTarget,
        app,
        create_decision_store,
    )

    settings = GatewaySettings(
        model_targets={
            "qwen": ModelTarget(
                url="http://primary/v1", input_cost_per_million=1.0, output_cost_per_million=2.0
            )
        }
    )
    upstream = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    app.state.settings = settings
    app.state.client = upstream
    app.state.backend_health = BackendHealthStore(settings, upstream)
    app.state.decision_store = create_decision_store(None)
    app.state.governance = None
    app.state.tenant_attribution = tenant_store
    app.state.budget_guard = guard
    app.state.budget_policy_error = None
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://gw")
    return app, client, upstream


@pytest.fixture
def _restore_gateway_state():
    from app.gateway.main import app

    yield
    app.state.budget_guard = None
    app.state.budget_policy_error = None


def _ok_handler(calls: list, usage: dict | None = None):
    import httpx

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        body = {"id": "x", "choices": [{"message": {"role": "assistant", "content": "hi"}}]}
        if usage is not None:
            body["usage"] = usage
        return httpx.Response(200, json=body, request=request)

    return handler


CHAT = {"model": "qwen", "messages": [{"role": "user", "content": "hello"}], "max_tokens": 100}


@pytest.mark.anyio
@pytest.mark.usefixtures("_restore_gateway_state")
async def test_chat_settles_actual_usage_and_flags_soft_warning() -> None:
    guard = InferenceBudgetGuard(POLICY)
    calls: list = []
    _app, client, upstream = _gateway(
        _ok_handler(calls, {"prompt_tokens": 10, "completion_tokens": 20}), guard
    )
    async with client:
        response = await client.post("/v1/chat/completions", json=CHAT)
    await upstream.aclose()
    assert response.status_code == 200, response.text
    assert response.headers["x-ai-budget-decision"] == "ALLOW"
    assert len(calls) == 1
    committed, reserved = await guard.snapshot_micros("tenant:platform")
    assert reserved == 0
    assert committed == 50  # 10*1 + 20*2 micro-USD from the gateway price table


@pytest.mark.anyio
@pytest.mark.usefixtures("_restore_gateway_state")
async def test_chat_soft_warning_header() -> None:
    guard = InferenceBudgetGuard(POLICY)
    # Tenant is "platform" without JWT (default $1.00 budget). Pre-fill 0.7999 so this
    # request crosses the 0.8 soft-warning ratio.
    _, held = await guard.reserve(
        tenant_id="platform", model="qwen", input_tokens=0, output_tokens=0, estimated_usd=0.7999
    )
    assert held is not None
    calls: list = []
    _app, client, upstream = _gateway(_ok_handler(calls, {"prompt_tokens": 1}), guard)
    async with client:
        response = await client.post("/v1/chat/completions", json=CHAT)
    await upstream.aclose()
    assert response.status_code == 200
    assert response.headers["x-ai-budget-warning"] == "soft_threshold"


@pytest.mark.anyio
@pytest.mark.usefixtures("_restore_gateway_state")
async def test_chat_without_usage_charges_estimate_not_zero() -> None:
    guard = InferenceBudgetGuard(POLICY)
    calls: list = []
    _app, client, upstream = _gateway(_ok_handler(calls, None), guard)
    async with client:
        response = await client.post("/v1/chat/completions", json=CHAT)
    await upstream.aclose()
    assert response.status_code == 200
    committed, reserved = await guard.snapshot_micros("tenant:platform")
    # estimate: 1 input token * $1/M + 100 output * $2/M = 201 micro-USD
    assert (committed, reserved) == (201, 0)


@pytest.mark.anyio
@pytest.mark.usefixtures("_restore_gateway_state")
async def test_chat_over_budget_is_429_and_backend_not_called() -> None:
    guard = InferenceBudgetGuard(POLICY)
    calls: list = []
    _app, client, upstream = _gateway(_ok_handler(calls), guard)
    big = {**CHAT, "max_tokens": 1_000_000}
    async with client:
        response = await client.post("/v1/chat/completions", json=big)
    await upstream.aclose()
    assert response.status_code == 429
    assert response.headers["x-ai-budget-reason"] == "budget_exceeded"
    assert calls == []


@pytest.mark.anyio
@pytest.mark.usefixtures("_restore_gateway_state")
@pytest.mark.parametrize("mode", ["unavailable", "timeout", "corrupt"])
async def test_chat_store_fault_is_503_and_backend_not_called(mode: str) -> None:
    guard = InferenceBudgetGuard(POLICY, redis=fakeredis.aioredis.FakeRedis(decode_responses=True))
    getattr(guard, f"simulate_store_{mode}")(True)
    calls: list = []
    _app, client, upstream = _gateway(_ok_handler(calls), guard)
    async with client:
        response = await client.post("/v1/chat/completions", json=CHAT)
    await upstream.aclose()
    assert response.status_code == 503
    assert response.headers["x-ai-budget-decision"] == "DENY"
    assert calls == []


@pytest.mark.anyio
@pytest.mark.usefixtures("_restore_gateway_state")
async def test_chat_malformed_policy_is_503() -> None:
    calls: list = []
    app, client, upstream = _gateway(_ok_handler(calls), None)
    app.state.budget_policy_error = "malformed inference budget policy: boom"
    async with client:
        response = await client.post("/v1/chat/completions", json=CHAT)
    await upstream.aclose()
    assert response.status_code == 503
    assert response.headers["x-ai-budget-reason"] == "malformed_policy"
    assert calls == []


@pytest.mark.anyio
@pytest.mark.usefixtures("_restore_gateway_state")
async def test_chat_releases_budget_when_later_admission_step_fails() -> None:
    from fastapi import HTTPException

    class _QuotaDeny:
        def resolve_team(self, request):
            return "finance"

        async def reserve_request(self, team, **kwargs):
            raise HTTPException(status_code=429, detail="quota")

    guard = InferenceBudgetGuard(POLICY)
    calls: list = []
    _app, client, upstream = _gateway(_ok_handler(calls), guard, tenant_store=_QuotaDeny())
    async with client:
        response = await client.post("/v1/chat/completions", json=CHAT)
    await upstream.aclose()
    assert response.status_code == 429
    assert calls == []
    assert await guard.snapshot_micros("tenant:platform") == (0, 0)


@pytest.mark.anyio
@pytest.mark.usefixtures("_restore_gateway_state")
async def test_chat_connect_error_releases_reservation() -> None:
    import httpx

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    guard = InferenceBudgetGuard(POLICY)
    _app, client, upstream = _gateway(handler, guard)
    async with client:
        response = await client.post("/v1/chat/completions", json=CHAT)
    await upstream.aclose()
    assert response.status_code == 502
    assert await guard.snapshot_micros("tenant:platform") == (0, 0)


@pytest.mark.anyio
@pytest.mark.usefixtures("_restore_gateway_state")
async def test_chat_post_execution_error_charges_estimate() -> None:
    import httpx

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"not json", request=request)

    guard = InferenceBudgetGuard(POLICY)
    _app, client, upstream = _gateway(handler, guard)
    async with client:
        with pytest.raises(json.JSONDecodeError):
            await client.post("/v1/chat/completions", json=CHAT)
    await upstream.aclose()
    # Model ran, response unusable: charged at the estimate, nothing left reserved.
    assert await guard.snapshot_micros("tenant:platform") == (201, 0)


@pytest.mark.anyio
@pytest.mark.usefixtures("_restore_gateway_state")
async def test_chat_stream_without_usage_settles_at_estimate() -> None:
    import httpx

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=b"data: hello\n\ndata: [DONE]\n\n",
            headers={"content-type": "text/event-stream"},
            request=request,
        )

    guard = InferenceBudgetGuard(POLICY)
    _app, client, upstream = _gateway(handler, guard)
    async with client:
        response = await client.post(
            "/v1/chat/completions",
            json={**CHAT, "stream": True},
        )
        assert response.status_code == 200
    await upstream.aclose()
    assert await guard.snapshot_micros("tenant:platform") == (201, 0)


@pytest.mark.anyio
@pytest.mark.usefixtures("_restore_gateway_state")
@pytest.mark.parametrize("max_tokens", ["lots", None, -5, True, [1]])
async def test_chat_malformed_max_tokens_does_not_500(max_tokens) -> None:
    guard = InferenceBudgetGuard(POLICY)
    calls: list = []
    _app, client, upstream = _gateway(_ok_handler(calls, {"prompt_tokens": 1}), guard)
    async with client:
        response = await client.post(
            "/v1/chat/completions",
            json={**CHAT, "max_tokens": max_tokens},
        )
    await upstream.aclose()
    assert response.status_code == 200


@pytest.mark.anyio
@pytest.mark.usefixtures("_restore_gateway_state")
async def test_chat_guard_off_by_default_has_no_budget_headers() -> None:
    calls: list = []
    _app, client, upstream = _gateway(_ok_handler(calls, {"prompt_tokens": 1}), None)
    async with client:
        response = await client.post("/v1/chat/completions", json=CHAT)
    await upstream.aclose()
    assert response.status_code == 200
    assert "x-ai-budget-decision" not in response.headers


# ------------------- real Redis, real parallelism (multiple OS processes) ---------------

MP_PROCESSES = 8
MP_PER_PROCESS = 25
MP_ROUNDS = int(os.environ.get("BUDGET_GUARD_MP_ROUNDS", "10"))
MP_BUDGET_MICROS = 1_000_000  # default scope budget $1.00
MP_ESTIMATE_MICROS = 7_000


def _mp_reserve_worker(url: str, start_at: float, queue) -> None:
    """Child process: own event loop, own Redis connection pool, hold reservations."""

    async def run() -> list[str]:
        from redis.asyncio import Redis

        client = Redis.from_url(url, decode_responses=True, max_connections=MP_PER_PROCESS)
        guard = InferenceBudgetGuard(POLICY, redis=client, store_timeout_seconds=5.0)
        await client.ping()
        while time.time() < start_at:
            await asyncio.sleep(0.001)

        async def one() -> str:
            decision, _ = await guard.reserve(
                tenant_id="mp",
                model="qwen",
                input_tokens=0,
                output_tokens=0,
                estimated_usd=MP_ESTIMATE_MICROS / 1_000_000,
            )
            return decision.decision if decision.status_code != 503 else "ERROR"

        out = await asyncio.gather(*[one() for _ in range(MP_PER_PROCESS)])
        await client.aclose()
        return out

    queue.put(asyncio.run(run()))


def _mp_settle_worker(url: str, start_at: float, seed: int, queue) -> None:
    """Child process: reserve → (settle | release | abandon) loops; report spend."""
    import random

    async def run() -> tuple[int, int, int]:
        from redis.asyncio import Redis

        rng = random.Random(seed)
        client = Redis.from_url(url, decode_responses=True, max_connections=MP_PER_PROCESS)
        guard = InferenceBudgetGuard(POLICY, redis=client, store_timeout_seconds=5.0)
        await client.ping()
        while time.time() < start_at:
            await asyncio.sleep(0.001)
        committed = 0
        allows = 0
        errors = 0

        async def one() -> None:
            nonlocal committed, allows, errors
            for _ in range(8):
                decision, res = await guard.reserve(
                    tenant_id="mp-settle",
                    model="qwen",
                    input_tokens=0,
                    output_tokens=0,
                    estimated_usd=MP_ESTIMATE_MICROS / 1_000_000,
                )
                if decision.status_code == 503:
                    errors += 1
                    continue
                if res is None:
                    continue
                allows += 1
                await asyncio.sleep(rng.random() / 200)
                roll = rng.random()
                if roll < 0.5:
                    actual = rng.randint(0, MP_ESTIMATE_MICROS)
                    await res.settle(actual / 1_000_000)
                    committed += actual
                elif roll < 0.75:
                    await res.release()  # failed before execution
                else:
                    res.mark_executed()
                    await res.abandon()  # failed after execution → estimate
                    committed += MP_ESTIMATE_MICROS
                await res.settle(0.0)  # duplicate settle must be a no-op

        await asyncio.gather(*[one() for _ in range(MP_PER_PROCESS)])
        await client.aclose()
        return committed, allows, errors

    queue.put(asyncio.run(run()))


def _run_processes(target, args_for) -> list:
    import multiprocessing as mp

    ctx = mp.get_context("spawn")
    queue = ctx.Queue()
    start_at = time.time() + 2.0
    procs = [
        ctx.Process(target=target, args=(*args_for(i, start_at), queue))
        for i in range(MP_PROCESSES)
    ]
    for proc in procs:
        proc.start()
    results = [queue.get(timeout=60) for _ in procs]
    for proc in procs:
        proc.join(timeout=30)
        assert proc.exitcode == 0
    return results


@pytest.mark.skipif(not REAL_REDIS_URL, reason="needs BUDGET_GUARD_REDIS_URL (real Redis)")
def test_real_redis_multiprocess_reserve_never_overspends() -> None:
    import redis as redis_sync

    sync = redis_sync.Redis.from_url(REAL_REDIS_URL, decode_responses=True)
    max_allows = MP_BUDGET_MICROS // MP_ESTIMATE_MICROS
    attempts = MP_PROCESSES * MP_PER_PROCESS
    for round_idx in range(MP_ROUNDS):
        sync.flushdb()
        results = _run_processes(_mp_reserve_worker, lambda i, s: (REAL_REDIS_URL, s))
        flat = [o for r in results for o in r]
        allows, denies, errors = flat.count("ALLOW"), flat.count("DENY"), flat.count("ERROR")
        window = sync.hgetall("arp:budget:tenant:mp:window")
        reserved = int(window["reserved_micros"])
        committed = int(window["committed_micros"])
        live = sync.zcard("arp:budget:tenant:mp:reservations")
        assert errors == 0
        assert allows + denies == attempts
        # Exactly the budget-limited count: no overspend and no leaked/lost capacity.
        assert allows == max_allows
        assert live == allows
        assert reserved == allows * MP_ESTIMATE_MICROS
        assert committed + reserved <= MP_BUDGET_MICROS
        CONCURRENCY_RESULTS.append(
            {
                "backend": "redis-real-multiprocess",
                "round": round_idx,
                "processes": MP_PROCESSES,
                "attempts": attempts,
                "allows": allows,
                "denies": denies,
                "max_allows": max_allows,
                "reserved_micros": reserved,
                "committed_micros": committed,
                "budget_micros": MP_BUDGET_MICROS,
            }
        )
    decision = BudgetDecisionForEvidence(
        decision="ALLOW",
        reason="concurrency_invariant_held",
        scope="tenant:mp",
        estimated_usd=MP_ESTIMATE_MICROS / 1_000_000,
        reserved_usd=max_allows * MP_ESTIMATE_MICROS / 1_000_000,
        committed_usd=0.0,
        budget_usd=MP_BUDGET_MICROS / 1_000_000,
        policy_version=POLICY.version,
        policy_sha256=POLICY.sha256(),
    )
    _record(
        "concurrency_atomic_redis_real_multiprocess",
        decision,
        rounds=MP_ROUNDS,
        processes=MP_PROCESSES,
        attempts_per_round=attempts,
        allows_per_round=max_allows,
    )


@pytest.mark.skipif(not REAL_REDIS_URL, reason="needs BUDGET_GUARD_REDIS_URL (real Redis)")
def test_real_redis_multiprocess_settle_is_exact() -> None:
    import redis as redis_sync

    sync = redis_sync.Redis.from_url(REAL_REDIS_URL, decode_responses=True)
    for round_idx in range(MP_ROUNDS):
        sync.flushdb()
        results = _run_processes(
            _mp_settle_worker, lambda i, s, r=round_idx: (REAL_REDIS_URL, s, r * 100 + i)
        )
        expected_committed = sum(r[0] for r in results)
        allows = sum(r[1] for r in results)
        errors = sum(r[2] for r in results)
        window = sync.hgetall("arp:budget:tenant:mp-settle:window")
        assert errors == 0
        assert allows > 0
        # No leaked reservation and no double-counted or lost settle.
        assert int(window["reserved_micros"]) == 0
        assert sync.zcard("arp:budget:tenant:mp-settle:reservations") == 0
        assert int(window["committed_micros"]) == expected_committed
        assert expected_committed <= MP_BUDGET_MICROS
        CONCURRENCY_RESULTS.append(
            {
                "backend": "redis-real-multiprocess-settle",
                "round": round_idx,
                "processes": MP_PROCESSES,
                "allows": allows,
                "committed_micros": int(window["committed_micros"]),
                "expected_committed_micros": expected_committed,
                "reserved_micros_after": int(window["reserved_micros"]),
                "budget_micros": MP_BUDGET_MICROS,
            }
        )


from app.gateway.budget_guard import BudgetDecision as BudgetDecisionForEvidence  # noqa: E402


def _concurrency_summary() -> dict:
    rows = CONCURRENCY_RESULTS
    over = 0
    for row in rows:
        if "total_usd" in row:
            if usd_to_micros(row["total_usd"]) > usd_to_micros(row["budget_usd"]):
                over += 1
        elif row.get("committed_micros", 0) + row.get("reserved_micros", 0) > row["budget_micros"]:
            over += 1
    real = [r for r in rows if r["backend"] == "redis-real-multiprocess"]
    settle = [r for r in rows if r["backend"] == "redis-real-multiprocess-settle"]
    return {
        "invariant": "committed_plus_reserved_le_budget (integer micro-USD)",
        "overspend_events": over,
        "single_process": {
            "backends": ["memory", "fakeredis"],
            "rounds_per_backend": 25,
            "parallel_requests": 40,
            "note": "asyncio.gather in one process; fakeredis serialises calls",
        },
        "real_redis_multiprocess": {
            "rounds": len(real),
            "processes": MP_PROCESSES,
            "attempts_per_round": MP_PROCESSES * MP_PER_PROCESS,
            "allows_per_round": sorted({r["allows"] for r in real}),
            "expected_allows_per_round": MP_BUDGET_MICROS // MP_ESTIMATE_MICROS,
            "max_reserved_micros": max((r["reserved_micros"] for r in real), default=0),
            "budget_micros": MP_BUDGET_MICROS,
        },
        "real_redis_multiprocess_settle": {
            "rounds": len(settle),
            "allows_total": sum(r["allows"] for r in settle),
            "exact_committed_rounds": sum(
                1 for r in settle if r["committed_micros"] == r["expected_committed_micros"]
            ),
            "leaked_reserved_micros": sum(r["reserved_micros_after"] for r in settle),
        },
        "results": rows,
    }


def test_zzz_write_evidence_artifacts() -> None:
    """Persist envelopes + concurrency stats for CI docs (runs last by name)."""
    # Ensure core scenarios already recorded by prior tests in this session.
    scenario_ids = {row["scenario_id"] for row in EVIDENCE_ENVELOPES}
    required = {
        "legitimate_within_budget",
        "over_budget_denied",
        "settlement_refunds_unused",
        "expired_reservation_released",
        "store_down_fail_closed",
        "store_timeout_fail_closed",
        "store_corrupt_fail_closed",
        "malformed_policy_fail_closed",
        "concurrency_atomic_memory",
        "concurrency_atomic_redis",
    }
    if REAL_REDIS_URL:
        required.add("concurrency_atomic_redis_real_multiprocess")
    missing = required - scenario_ids
    assert not missing, f"missing evidence scenarios: {missing}"

    for row in EVIDENCE_ENVELOPES:
        for key in (
            "schema_version",
            "policy_version",
            "policy_sha256",
            "decision",
            "reason",
            "trace_id",
            "commit_sha",
            "timestamp",
            "envelope_sha256",
        ):
            assert key in row
        assert row["decision"] in {"ALLOW", "DENY"}
        assert row["schema_version"] == "1.0.0"

    # False-deny count on legitimate allows in this suite (computed, not asserted-in).
    false_denies = sum(
        1
        for row in EVIDENCE_ENVELOPES
        if row["scenario_id"] == "legitimate_within_budget" and row["decision"] == "DENY"
    )
    assert false_denies == 0

    # Only the evidence generator writes artifacts; a plain `pytest` run must not
    # modify tracked docs.
    evidence_dir = os.environ.get("BUDGET_EVIDENCE_DIR", "").strip()
    if not evidence_dir:
        return
    out_dir = Path(evidence_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    results_path = out_dir / "budget_guard_results.json"
    payload = {
        "suite": "inference_budget_guard",
        "scope": "on this test suite",
        "policy_version": POLICY.version,
        "policy_sha256": POLICY.sha256(),
        "commit_sha": os.environ.get("GITHUB_SHA", "unknown"),
        "envelopes": EVIDENCE_ENVELOPES,
        "concurrency": _concurrency_summary(),
        "false_deny_count_legitimate": false_denies,
        "limits": [
            "Claims apply on this test suite / this setup only.",
            "Pre-request estimate can differ from actual token usage.",
            "Settlement can lag if the store is unavailable after upstream success; "
            "expired reservations are released by TTL.",
            "Unknown actual usage (no usage block, e.g. most streams) is charged at the "
            "estimate; actual spend above the estimate is committed as-is, so committed "
            "spend can exceed the budget by at most the in-flight estimate error.",
            "Estimate prices come from the budget policy; settlement prices from gateway "
            "model targets. Keep the two tables aligned.",
            "Reservation expiry uses each gateway's wall clock; large clock skew between "
            "replicas shifts TTL release.",
            "In-memory backend is per process. Lua atomicity is exercised against a real "
            "Redis server (CI service container) with multiple OS processes — one Redis, "
            "not multi-region billing.",
        ],
    }
    results_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
