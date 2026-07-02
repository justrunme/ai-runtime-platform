"""Tests for intent resolution proxy."""

from __future__ import annotations

import httpx
import pytest

from app.gateway.governance import GovernanceConfig
from app.gateway.intent import build_intent_payload, resolve_intent


def _config() -> GovernanceConfig:
    return GovernanceConfig(
        control_plane_url="http://control-plane.test",
        enabled=True,
        fail_open=False,
        timeout_seconds=1.0,
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


def test_build_intent_payload_includes_message() -> None:
    from starlette.requests import Request

    scope = {
        "type": "http",
        "headers": [(b"x-ai-team", b"finance")],
        "method": "POST",
        "path": "/v1/intent/resolve",
        "query_string": b"",
        "client": ("testclient", 50000),
        "server": ("testserver", 80),
        "scheme": "http",
        "root_path": "",
    }
    request = Request(scope)
    payload = build_intent_payload(
        request,
        {"message": "Generate quarterly report"},
        _config(),
    )
    assert payload["message"] == "Generate quarterly report"
    assert payload["team"] == "finance"


@pytest.mark.anyio
async def test_resolve_intent_calls_control_plane() -> None:
    from starlette.requests import Request

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/intent/resolve":
            body = request.read().decode()
            assert "quarterly" in body
            return httpx.Response(
                200,
                json={
                    "intent": "finance_report",
                    "confidence": 0.5,
                    "forbidden": False,
                    "reasons": [],
                    "plan": {
                        "agent": "finance-copilot",
                        "model": "llama3.1:8b",
                        "tools": ["jira-read"],
                        "region": "eu-central",
                        "runtime": "ollama",
                        "cluster": "eu-prod",
                        "policy_pack": "production",
                        "namespace": "ai-prod",
                        "team": "finance",
                    },
                },
            )
        return httpx.Response(404)

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        scope = {
            "type": "http",
            "headers": [(b"x-ai-team", b"finance")],
            "method": "POST",
            "path": "/v1/intent/resolve",
            "query_string": b"",
            "client": ("testclient", 50000),
            "server": ("testserver", 80),
            "scheme": "http",
            "root_path": "",
        }
        request = Request(scope)
        result = await resolve_intent(
            client,
            _config(),
            request,
            {"message": "Generate quarterly revenue report"},
        )
        assert result["intent"] == "finance_report"
