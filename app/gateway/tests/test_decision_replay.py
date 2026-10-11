"""Single-use decision token (jti) replay protection."""

from __future__ import annotations

import time

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import HTTPException

from app.gateway.decision_replay import (
    InMemoryDecisionReplayStore,
    RedisDecisionReplayStore,
    create_decision_replay_store,
    derive_jti,
)
from app.gateway.decision_token import bind_signed_decision
from app.gateway.request_digest import compute_request_digest


@pytest.mark.anyio
async def test_memory_replay_store_rejects_second_consume() -> None:
    store = InMemoryDecisionReplayStore()
    assert await store.consume("jti-1", tenant="finance", request_digest="abc", ttl_seconds=60)
    assert (
        await store.consume("jti-1", tenant="finance", request_digest="abc", ttl_seconds=60)
        is False
    )


@pytest.mark.anyio
async def test_redis_replay_store_rejects_second_consume() -> None:
    import fakeredis.aioredis

    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    store = RedisDecisionReplayStore(redis)
    assert await store.consume("jti-1", tenant="finance", request_digest="abc", ttl_seconds=60)
    assert await store.consume("jti-1", tenant="ops", request_digest="xyz", ttl_seconds=60) is False
    await redis.aclose()


def test_derive_jti_prefers_explicit_claim() -> None:
    assert derive_jti(claims={"jti": "unique", "decision_id": "dec"}, token="t") == "unique"
    assert derive_jti(claims={"decision_id": "dec"}, token="t") == "dec"
    assert len(derive_jti(claims={}, token="raw-token")) == 64


@pytest.mark.anyio
async def test_bind_signed_decision_rejects_replay(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)

    class _Key:
        def __init__(self, key):
            self.key = key

    class _Client:
        def __init__(self, *args, **kwargs):
            pass

        def get_signing_key_from_jwt(self, token: str):
            return _Key(private_key.public_key())

    monkeypatch.setenv("CONTROL_PLANE_DECISION_JWKS_URL", "https://example.test/jwks")
    monkeypatch.setenv("CONTROL_PLANE_DECISION_AUDIENCE", "ai-runtime")
    monkeypatch.setattr("app.gateway.decision_token.PyJWKClient", _Client)
    monkeypatch.setattr(
        "app.gateway.decision_token.get_decision_jwks_client",
        lambda url: _Client(),
    )

    evaluate_payload = {
        "subject": "alice",
        "groups": ["finance"],
        "team": "finance",
        "tenant_id": "finance",
        "owner": "alice",
        "environment": "production",
        "namespace": "ai-prod",
        "action": "invoke_model",
        "model": "qwen",
        "provider": "vllm",
        "input_tokens": 10,
        "output_tokens": 5,
        "cost_per_request_usd": 0.01,
        "cost_per_hour_usd": 1.0,
        "month_to_date_cost_usd": 10.0,
        "forecast_monthly_cost_usd": 20.0,
        "sensitive_data": False,
        "tool_access": False,
        "write_permission": False,
        "policy_pack": "production",
    }
    digest = compute_request_digest(evaluate_payload)
    token = jwt.encode(
        {
            "jti": "once-only",
            "decision_id": "dec_123",
            "verdict": "allow",
            "request_digest": f"sha256:{digest}",
            "tenant": "finance",
            "aud": "ai-runtime",
            "exp": int(time.time()) + 60,
        },
        private_key,
        algorithm="RS256",
        headers={"kid": "cp-decision"},
    )
    store = InMemoryDecisionReplayStore()
    result = {
        "final_verdict": "allow",
        "decision_id": "dec_123",
        "decision_token": token,
        "request_digest": f"sha256:{digest}",
    }
    ok = await bind_signed_decision(result, evaluate_payload=evaluate_payload, replay_store=store)
    assert ok["decision_id"] == "dec_123"
    with pytest.raises(HTTPException) as error:
        await bind_signed_decision(result, evaluate_payload=evaluate_payload, replay_store=store)
    assert error.value.status_code == 403
    assert error.value.detail["error"]["code"] == "decision_replayed"


@pytest.mark.anyio
async def test_bind_signed_decision_binds_tenant_mismatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)

    class _Key:
        def __init__(self, key):
            self.key = key

    class _Client:
        def __init__(self, *args, **kwargs):
            pass

        def get_signing_key_from_jwt(self, token: str):
            return _Key(private_key.public_key())

    monkeypatch.setenv("CONTROL_PLANE_DECISION_JWKS_URL", "https://example.test/jwks")
    monkeypatch.setenv("CONTROL_PLANE_DECISION_AUDIENCE", "ai-runtime")
    monkeypatch.setattr("app.gateway.decision_token.PyJWKClient", _Client)
    monkeypatch.setattr(
        "app.gateway.decision_token.get_decision_jwks_client",
        lambda url: _Client(),
    )

    token = jwt.encode(
        {
            "jti": "bound-1",
            "decision_id": "dec_1",
            "verdict": "allow",
            "tenant": "finance",
            "aud": "ai-runtime",
            "exp": int(time.time()) + 60,
        },
        private_key,
        algorithm="RS256",
        headers={"kid": "cp-decision"},
    )
    with pytest.raises(HTTPException) as error:
        await bind_signed_decision(
            {"final_verdict": "allow", "decision_id": "dec_1", "decision_token": token},
            evaluate_payload={"team": "ops", "tenant_id": "ops"},
            replay_store=InMemoryDecisionReplayStore(),
        )
    assert error.value.detail["error"]["code"] == "signed_decision_mismatch"


def test_create_decision_replay_store(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GATEWAY_DECISION_REPLAY_PROTECTION", "false")
    assert create_decision_replay_store(None) is None
    monkeypatch.setenv("GATEWAY_DECISION_REPLAY_PROTECTION", "true")
    assert isinstance(create_decision_replay_store(None), InMemoryDecisionReplayStore)
    store = create_decision_replay_store("redis://localhost:6379/0")
    assert isinstance(store, RedisDecisionReplayStore)
