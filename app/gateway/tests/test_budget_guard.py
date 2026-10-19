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
@pytest.mark.parametrize("backend", ["memory", "redis"])
async def test_concurrency_never_overspends(backend: str) -> None:
    """N parallel reserves: total reserved ≤ budget. Run many times."""
    budget_usd = 0.10
    # Each reserve costs $0.02 → at most 5 allows.
    estimate = 0.02
    n = 40
    rounds = 25
    max_allows = int(budget_usd // estimate)

    for round_idx in range(rounds):
        if backend == "redis":
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
                f"concurrency_atomic_{backend}",
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

    # False-deny count on legitimate allows in this suite.
    legit = [
        row
        for row in EVIDENCE_ENVELOPES
        if row["scenario_id"] == "legitimate_within_budget" and row["decision"] == "DENY"
    ]
    assert len(legit) == 0

    out_dir = Path(os.environ.get("BUDGET_EVIDENCE_DIR", "docs"))
    out_dir.mkdir(parents=True, exist_ok=True)
    results_path = out_dir / "budget_guard_results.json"
    payload = {
        "suite": "inference_budget_guard",
        "scope": "on this test suite",
        "policy_version": POLICY.version,
        "policy_sha256": POLICY.sha256(),
        "commit_sha": os.environ.get("GITHUB_SHA", "unknown"),
        "envelopes": EVIDENCE_ENVELOPES,
        "concurrency": {
            "rounds_per_backend": 25,
            "parallel_requests": 40,
            "results": CONCURRENCY_RESULTS,
            "invariant": "total_reserved_plus_committed_le_budget",
            "max_total_usd_observed": max(
                (row["total_usd"] for row in CONCURRENCY_RESULTS), default=0.0
            ),
            "budget_usd": 0.10,
            "overspend_events": sum(
                1 for row in CONCURRENCY_RESULTS if row["total_usd"] > row["budget_usd"] + 1e-9
            ),
        },
        "false_deny_count_legitimate": 0,
        "limits": [
            "Claims apply on this test suite / this setup only.",
            "Pre-request estimate can differ from actual token usage.",
            "Settlement can lag if the store is unavailable after upstream success; "
            "expired reservations are released by TTL.",
            "In-memory backend proves single-process atomicity; Redis/fakeredis proves "
            "Lua atomicity on this CI setup — not multi-region billing.",
        ],
    }
    results_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
