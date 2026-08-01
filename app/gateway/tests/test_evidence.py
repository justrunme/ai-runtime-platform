"""Enforcement evidence headers and decision-record correlation."""

from __future__ import annotations

import time

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import HTTPException

from app.gateway.decision_token import bind_signed_decision
from app.gateway.decisions import DecisionRecord, InMemoryDecisionStore
from app.gateway.evidence import (
    apply_evidence_headers,
    evidence_from_governance,
    runtime_version,
)


def test_evidence_headers_do_not_touch_openai_body() -> None:
    evidence = evidence_from_governance(
        {
            "decision_id": "dec_123",
            "policy_bundle_id": "production-2026-08",
            "policy_digest": "sha256:abc",
            "request_digest": "sha256:req",
            "control_plane_version": "1.4.0",
        },
        enforcement_outcome="executed",
    )
    headers = apply_evidence_headers({}, evidence)
    assert headers["x-ai-control-decision-id"] == "dec_123"
    assert headers["x-ai-decision-id"] == "dec_123"
    assert headers["x-ai-policy-bundle-id"] == "production-2026-08"
    assert headers["x-ai-policy-digest"] == "sha256:abc"
    assert headers["x-ai-request-digest"] == "sha256:req"
    assert headers["x-ai-runtime-version"] == runtime_version()
    # Body stays untouched — headers only.
    body = {"id": "chatcmpl-1", "choices": []}
    assert "decision_id" not in body


@pytest.mark.anyio
async def test_decision_store_persists_enforcement_evidence() -> None:
    store = InMemoryDecisionStore()
    await store.put(
        DecisionRecord(
            request_id="req-ev",
            requested_model="qwen",
            selected_backend="qwen",
            routing_reason="primary",
            fallback_used=False,
            health_score=100,
            duration_ms=12.0,
            control_plane_decision_id="dec_123",
            policy_bundle_id="production-2026-08",
            policy_digest="sha256:abc",
            request_digest="sha256:req",
            control_plane_version="1.4.0",
            runtime_version="1.4.0",
            enforcement_outcome="executed",
        )
    )
    record = await store.get("req-ev")
    assert record is not None
    assert record.control_plane_decision_id == "dec_123"
    assert record.policy_digest == "sha256:abc"
    assert record.request_digest == "sha256:req"
    assert record.enforcement_outcome == "executed"


@pytest.mark.anyio
async def test_bind_signed_decision_optional_when_absent() -> None:
    result = await bind_signed_decision({"final_verdict": "allow", "decision_id": "dec_1"})
    assert result["decision_id"] == "dec_1"


@pytest.mark.anyio
async def test_bind_signed_decision_required_without_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GATEWAY_REQUIRE_SIGNED_DECISION", "true")
    with pytest.raises(HTTPException) as error:
        await bind_signed_decision({"final_verdict": "allow"})
    assert error.value.status_code == 503
    assert error.value.detail["error"]["code"] == "signed_decision_required"


@pytest.mark.anyio
async def test_bind_signed_decision_verifies_jws(monkeypatch: pytest.MonkeyPatch) -> None:
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

    token = jwt.encode(
        {
            "decision_id": "dec_123",
            "verdict": "allow",
            "request_digest": "sha256:req",
            "policy_digest": "sha256:pol",
            "aud": "ai-runtime",
            "exp": int(time.time()) + 60,
        },
        private_key,
        algorithm="RS256",
        headers={"kid": "cp-decision"},
    )
    result = await bind_signed_decision(
        {
            "final_verdict": "allow",
            "decision_id": "dec_123",
            "decision_token": token,
        }
    )
    assert result["request_digest"] == "sha256:req"
    assert result["policy_digest"] == "sha256:pol"
