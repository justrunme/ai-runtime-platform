import httpx
import pytest
from fastapi import HTTPException

from app.gateway.governance import (
    GovernanceConfig,
    build_evaluate_payload,
    enforce_governance,
)
from app.gateway.main import (
    BackendHealthStore,
    GatewaySettings,
    ModelTarget,
    app,
    create_decision_store,
)


def _governance_config(**overrides) -> GovernanceConfig:
    defaults = {
        "control_plane_url": "http://control-plane:8080",
        "enabled": True,
        "fail_open": False,
        "timeout_seconds": 2.0,
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


def test_governance_config_is_disabled_without_control_plane_url(monkeypatch) -> None:
    monkeypatch.delenv("CONTROL_PLANE_URL", raising=False)
    assert GovernanceConfig.from_environment() is None


def test_governance_config_reads_environment(monkeypatch) -> None:
    monkeypatch.setenv("CONTROL_PLANE_URL", "http://control-plane:8080/")
    monkeypatch.setenv("GOVERNANCE_FAIL_OPEN", "true")
    monkeypatch.setenv("GOVERNANCE_DEFAULT_TEAM", "research")
    config = GovernanceConfig.from_environment()
    assert config is not None
    assert config.control_plane_url == "http://control-plane:8080"
    assert config.fail_open is True
    assert config.default_team == "research"


def test_build_evaluate_payload_maps_headers_and_cost() -> None:
    request = httpx.Request(
        "POST",
        "http://gw/v1/chat/completions",
        headers={
            "x-ai-team": "ml-platform",
            "x-ai-sensitive-data": "true",
            "x-ai-forecast-monthly-cost-usd": "900",
        },
    )
    target = ModelTarget(
        url="http://model", input_cost_per_million=1.0, output_cost_per_million=2.0
    )
    payload = build_evaluate_payload(
        request,
        {"model": "qwen", "messages": [{"content": "hello world"}], "max_tokens": 100},
        _governance_config(),
        {"qwen": target},
    )
    assert payload["team"] == "ml-platform"
    assert payload["model"] == "qwen"
    assert payload["sensitive_data"] is True
    assert payload["forecast_monthly_cost_usd"] == 900.0
    assert payload["input_tokens"] >= 1
    assert payload["cost_per_request_usd"] > 0


@pytest.mark.anyio
async def test_enforce_governance_forwards_authorization_header() -> None:
    seen_authorization: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/governance/evaluate":
            seen_authorization.append(request.headers.get("authorization", ""))
            return httpx.Response(
                200,
                json={"final_verdict": "allow", "reasons": ["ok"], "stages": {}},
                request=request,
            )
        raise AssertionError(f"unexpected request: {request.url}")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        await enforce_governance(
            client,
            _governance_config(),
            httpx.Request(
                "POST",
                "http://gw/v1/chat/completions",
                headers={"authorization": "Bearer signed.jwt.token"},
            ),
            {"model": "qwen", "messages": []},
            {},
        )

    assert seen_authorization == ["Bearer signed.jwt.token"]


@pytest.mark.anyio
async def test_enforce_governance_allows_request() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/governance/evaluate":
            return httpx.Response(
                200,
                json={"final_verdict": "allow", "reasons": ["ok"], "stages": {}},
                request=request,
            )
        raise AssertionError(f"unexpected request: {request.url}")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await enforce_governance(
            client,
            _governance_config(),
            httpx.Request("POST", "http://gw/v1/chat/completions"),
            {"model": "qwen", "messages": []},
            {},
        )
    assert result is not None
    assert result["final_verdict"] == "allow"


@pytest.mark.anyio
async def test_enforce_governance_blocks_request() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "final_verdict": "block",
                "reasons": ["cost governance blocked the request"],
                "stages": {"cost": {"decision": "block"}},
            },
            request=request,
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(HTTPException) as error:
            await enforce_governance(
                client,
                _governance_config(),
                httpx.Request("POST", "http://gw/v1/chat/completions"),
                {"model": "qwen", "messages": []},
                {},
            )
    assert error.value.status_code == 403


@pytest.mark.anyio
async def test_enforce_governance_requires_approval() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "final_verdict": "approval_required",
                "reasons": ["critical risk score requires human approval"],
                "stages": {"risk": {"level": "critical"}},
            },
            request=request,
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(HTTPException) as error:
            await enforce_governance(
                client,
                _governance_config(),
                httpx.Request("POST", "http://gw/v1/chat/completions"),
                {"model": "qwen", "messages": []},
                {},
            )
    assert error.value.status_code == 409


@pytest.mark.anyio
async def test_enforce_governance_fail_closed_on_control_plane_error() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, request=_request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(HTTPException) as error:
            await enforce_governance(
                client,
                _governance_config(fail_open=False),
                httpx.Request("POST", "http://gw/v1/chat/completions"),
                {"model": "qwen", "messages": []},
                {},
            )
    assert error.value.status_code == 503


@pytest.mark.anyio
async def test_enforce_governance_fail_open_on_control_plane_error() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, request=_request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await enforce_governance(
            client,
            _governance_config(fail_open=True),
            httpx.Request("POST", "http://gw/v1/chat/completions"),
            {"model": "qwen", "messages": []},
            {},
        )
    assert result is None


@pytest.mark.anyio
async def test_chat_completions_calls_governance_before_upstream() -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        if request.url.path == "/governance/evaluate":
            return httpx.Response(
                200,
                json={"final_verdict": "allow", "reasons": ["ok"], "stages": {}},
                request=request,
            )
        return httpx.Response(200, json={"usage": {}}, request=request)

    settings = GatewaySettings(
        model_targets={
            "qwen": ModelTarget(
                url="http://primary/v1", input_cost_per_million=0, output_cost_per_million=0
            )
        }
    )
    upstream = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    app.state.settings = settings
    app.state.client = upstream
    app.state.backend_health = BackendHealthStore(settings, upstream)
    app.state.decision_store = create_decision_store(None)
    app.state.governance = _governance_config()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://gw") as client:
        response = await client.post(
            "/v1/chat/completions",
            json={"model": "qwen", "messages": []},
            headers={"x-ai-team": "research"},
        )
        metrics = await client.get("/metrics")
    assert response.status_code == 200
    assert calls[0] == "/governance/evaluate"
    assert "gateway_governance_decisions_total" in metrics.text
    assert 'verdict="allow"' in metrics.text
    await upstream.aclose()


@pytest.mark.anyio
async def test_chat_completions_returns_403_when_governance_blocks() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/governance/evaluate":
            return httpx.Response(
                200,
                json={
                    "final_verdict": "block",
                    "reasons": ["cost governance blocked the request"],
                    "stages": {},
                },
                request=request,
            )
        return httpx.Response(200, json={"usage": {}}, request=request)

    settings = GatewaySettings(
        model_targets={
            "qwen": ModelTarget(
                url="http://primary/v1", input_cost_per_million=0, output_cost_per_million=0
            )
        }
    )
    upstream = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    app.state.settings = settings
    app.state.client = upstream
    app.state.backend_health = BackendHealthStore(settings, upstream)
    app.state.decision_store = create_decision_store(None)
    app.state.governance = _governance_config()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://gw") as client:
        response = await client.post("/v1/chat/completions", json={"model": "qwen", "messages": []})
    assert response.status_code == 403
    assert response.json()["detail"]["final_verdict"] == "block"
    await upstream.aclose()
