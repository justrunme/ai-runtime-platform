"""Canonical request digest vectors for Control Plane alignment."""

from __future__ import annotations

import time

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import HTTPException

from app.gateway.decision_token import bind_signed_decision
from app.gateway.request_digest import compute_request_digest


def test_request_digest_ignores_live_telemetry() -> None:
    base = {
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
        "model_revision": "",
        "model_artifact_digest": "",
        "agent": "",
        "region": "",
        "policy_pack": "production",
    }
    left = {**base, "requests_last_minute": 1, "tokens_today": 100}
    right = {**base, "requests_last_minute": 99, "tokens_today": 9999}
    assert compute_request_digest(left) == compute_request_digest(right)


@pytest.mark.anyio
async def test_signed_decision_requires_local_request_digest_match(
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
            "decision_id": "dec_123",
            "verdict": "allow",
            "request_digest": digest,
            "policy_digest": "pol",
            "aud": "ai-runtime",
            "exp": int(time.time()) + 60,
        },
        private_key,
        algorithm="RS256",
        headers={"kid": "cp-decision"},
    )
    ok = await bind_signed_decision(
        {
            "final_verdict": "allow",
            "decision_id": "dec_123",
            "request_digest": digest,
            "policy_digest": "pol",
            "decision_token": token,
        },
        evaluate_payload=evaluate_payload,
    )
    assert ok["decision_id"] == "dec_123"

    replay_payload = {**evaluate_payload, "model": "llama"}
    with pytest.raises(HTTPException) as error:
        await bind_signed_decision(
            {
                "final_verdict": "allow",
                "decision_id": "dec_123",
                "request_digest": digest,
                "policy_digest": "pol",
                "decision_token": token,
            },
            evaluate_payload=replay_payload,
        )
    assert error.value.status_code == 403
    assert error.value.detail["error"]["code"] == "signed_decision_request_mismatch"
