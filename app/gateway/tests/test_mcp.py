"""Tests for MCP gateway enforcement."""

from __future__ import annotations

import httpx
import pytest
from fastapi import HTTPException

from app.gateway.governance import GovernanceConfig, build_evaluate_payload
from app.gateway.mcp import (
    enforce_tool_governance,
    governed_tool_response,
)
from app.gateway.main import GatewaySettings, ModelTarget, app, create_decision_store


def _governance_config(**overrides) -> GovernanceConfig:
    defaults = {
        "control_plane_url": "http://control-plane.test",
        "enabled": True,
        "fail_open": False,
        "timeout_seconds": 1.0,
        "default_team": "platform",
        "default_owner": "gateway",
        "default_environment": "development",
        "default_namespace": "ai-dev",
        "default_provider": "ollama",
        "default_action": "invoke_model",
        "default_cost_per_hour_usd": 0.18,
        "default_month_to_date_cost_usd": 100.0,
        "default_forecast_monthly_cost_usd": 400.0,
    }
    defaults.update(overrides)
    return GovernanceConfig(**defaults)


def _model_target() -> ModelTarget:
    return ModelTarget(
        url="http://ollama.test",
        input_cost_per_million=0.5,
        output_cost_per_million=1.0,
    )


def test_build_evaluate_payload_includes_prompt_text() -> None:
    from starlette.requests import Request

    scope = {
        "type": "http",
        "headers": [],
        "method": "POST",
        "path": "/v1/chat/completions",
        "query_string": b"",
        "client": ("testclient", 50000),
        "server": ("testserver", 80),
        "scheme": "http",
        "root_path": "",
    }
    request = Request(scope)
    payload = build_evaluate_payload(
        request,
        {
            "model": "llama3.1:8b",
            "messages": [{"role": "user", "content": "hello world"}],
        },
        _governance_config(),
        {"llama3.1:8b": _model_target()},
    )
    assert "hello world" in payload["prompt_text"]


@pytest.mark.anyio
async def test_enforce_tool_governance_blocks_delete() -> None:
    from starlette.requests import Request

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/governance/evaluate-tool":
            return httpx.Response(
                200,
                json={
                    "final_verdict": "block",
                    "reasons": ["action delete is forbidden for tool"],
                    "stages": {},
                },
            )
        return httpx.Response(404)

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        scope = {
            "type": "http",
            "headers": [(b"x-ai-team", b"platform")],
            "method": "POST",
            "path": "/mcp/tools/kubernetes-admin/call",
            "query_string": b"",
            "client": ("testclient", 50000),
            "server": ("testserver", 80),
            "scheme": "http",
            "root_path": "",
        }
        request = Request(scope)
        with pytest.raises(HTTPException) as exc:
            await enforce_tool_governance(
                client,
                _governance_config(),
                request,
                "kubernetes-admin",
                {"action": "delete", "arguments": {"resource": "pod/demo"}},
            )
        assert exc.value.status_code == 403


def test_governed_tool_response_shape() -> None:
    payload = governed_tool_response(
        "jira-read",
        {"action": "read", "arguments": {"issue": "PROJ-1"}},
        {"final_verdict": "allow"},
    )
    assert payload["tool"] == "jira-read"
    assert payload["status"] == "governed_stub"


@pytest.mark.anyio
async def test_mcp_tool_call_endpoint_blocks_delete() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/governance/evaluate-tool":
            return httpx.Response(
                200,
                json={
                    "final_verdict": "block",
                    "reasons": ["blocked"],
                    "stages": {},
                },
            )
        return httpx.Response(404)

    transport = httpx.MockTransport(handler)
    settings = GatewaySettings(
        model_targets={
            "llama3.1:8b": _model_target(),
        }
    )
    app.state.settings = settings
    app.state.governance = _governance_config()
    app.state.decision_store = create_decision_store(None)
    app.state.tenant_attribution = None

    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://test",
    ) as client:
        app.state.client = client
        from httpx import ASGITransport

        async with httpx.AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
        ) as api_client:
            response = await api_client.post(
                "/mcp/tools/kubernetes-admin/call",
                headers={"x-ai-team": "platform"},
                json={"action": "delete", "arguments": {"resource": "pod/demo"}},
            )
            assert response.status_code == 403
