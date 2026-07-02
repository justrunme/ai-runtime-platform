"""Tests for gateway workload identity resolution."""

from __future__ import annotations

import base64
import json

from starlette.requests import Request

from app.gateway.governance import GovernanceConfig, build_evaluate_payload
from app.gateway.identity import resolve_workload_identity


def _make_jwt(claims: dict) -> str:
    header = base64.urlsafe_b64encode(json.dumps({"alg": "none"}).encode()).decode().rstrip("=")
    payload = (
        base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
    )
    return f"{header}.{payload}.signature"


def _request(headers: dict[str, str] | None = None) -> Request:
    raw_headers = [
        (key.lower().encode(), value.encode())
        for key, value in (headers or {}).items()
    ]
    scope = {
        "type": "http",
        "method": "POST",
        "path": "/v1/chat/completions",
        "headers": raw_headers,
    }
    return Request(scope)


def test_resolve_identity_from_jwt() -> None:
    token = _make_jwt({"sub": "svc-1", "groups": ["platform"], "preferred_username": "bot"})
    identity = resolve_workload_identity(
        _request({"authorization": f"Bearer {token}"}),
        {
            "team": "finance",
            "owner": "gateway",
            "environment": "development",
            "namespace": "ai-dev",
        },
    )

    assert identity.subject == "svc-1"
    assert identity.team == "platform"
    assert identity.source == "jwt"


def test_build_evaluate_payload_includes_subject() -> None:
    config = GovernanceConfig(
        control_plane_url="http://control-api:8080",
        enabled=True,
        fail_open=False,
        timeout_seconds=2.0,
        default_team="platform",
        default_owner="gateway",
        default_environment="development",
        default_namespace="ai-dev",
        default_provider="ollama",
        default_action="invoke_model",
        default_cost_per_hour_usd=0.18,
        default_month_to_date_cost_usd=100.0,
        default_forecast_monthly_cost_usd=400.0,
    )
    payload = build_evaluate_payload(
        _request(
            {
                "x-ai-subject": "user-99",
                "x-ai-team": "search",
                "x-ai-owner": "dana",
            }
        ),
        {"model": "llama3.1:8b", "messages": [{"role": "user", "content": "hi"}]},
        config,
        {},
    )

    assert payload["subject"] == "user-99"
    assert payload["team"] == "search"
    assert payload["owner"] == "dana"
