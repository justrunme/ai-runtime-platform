"""Tests for post-response evaluation submission."""

from __future__ import annotations

from app.gateway.evaluations import build_evaluation_payload, response_evaluation_enabled


def test_build_evaluation_payload_extracts_response_text() -> None:
    payload = build_evaluation_payload(
        team="platform",
        model="llama3.1:8b",
        request_id="req-1",
        chat_payload={
            "messages": [{"role": "user", "content": "hello"}],
        },
        completion={
            "choices": [{"message": {"content": "Hello there!"}}],
        },
        latency_ms=120.5,
        cost_usd=0.002,
    )
    assert payload["response_text"] == "Hello there!"
    assert "hello" in payload["prompt_text"]


def test_response_evaluation_disabled_by_default(monkeypatch) -> None:
    monkeypatch.delenv("GOVERNANCE_EVALUATE_RESPONSE", raising=False)
    assert response_evaluation_enabled() is False
