import json

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
from app.gateway.readiness import build_readiness_report


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
        "mode": "enforce",
    }
    defaults.update(overrides)
    return GovernanceConfig(**defaults)


def _bootstrap_app(
    *,
    governance: GovernanceConfig | None,
    handler=None,
) -> httpx.AsyncClient:
    def default_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"usage": {}}, request=request)

    settings = GatewaySettings(
        model_targets={
            "qwen": ModelTarget(
                url="http://primary/v1", input_cost_per_million=0, output_cost_per_million=0
            )
        }
    )
    upstream = httpx.AsyncClient(transport=httpx.MockTransport(handler or default_handler))
    app.state.settings = settings
    app.state.client = upstream
    app.state.backend_health = BackendHealthStore(settings, upstream)
    app.state.decision_store = create_decision_store(None)
    app.state.governance = governance
    app.state.tenant_attribution = None
    app.state.decision_replay = None
    return upstream


def test_governance_enforce_without_control_plane_url_is_misconfigured(monkeypatch) -> None:
    monkeypatch.delenv("CONTROL_PLANE_URL", raising=False)
    monkeypatch.delenv("GOVERNANCE_MODE", raising=False)
    config = GovernanceConfig.from_environment()
    assert config.mode == "enforce"
    assert config.control_plane_url == ""
    assert config.misconfigured is True
    assert config.enabled is True


def test_governance_disabled_mode_requires_explicit_env(monkeypatch) -> None:
    monkeypatch.delenv("CONTROL_PLANE_URL", raising=False)
    monkeypatch.setenv("GOVERNANCE_MODE", "disabled")
    config = GovernanceConfig.from_environment()
    assert config.mode == "disabled"
    assert config.enabled is False
    assert config.misconfigured is False


def test_governance_rejects_unknown_mode(monkeypatch) -> None:
    monkeypatch.setenv("GOVERNANCE_MODE", "soft")
    with pytest.raises(ValueError, match="GOVERNANCE_MODE"):
        GovernanceConfig.from_environment()


def test_governance_config_reads_environment(monkeypatch) -> None:
    monkeypatch.setenv("GOVERNANCE_MODE", "enforce")
    monkeypatch.setenv("CONTROL_PLANE_URL", "http://control-plane:8080/")
    monkeypatch.setenv("GOVERNANCE_FAIL_OPEN", "true")
    monkeypatch.setenv("GOVERNANCE_DEFAULT_TEAM", "research")
    config = GovernanceConfig.from_environment()
    assert config is not None
    assert config.mode == "enforce"
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
        requests_last_minute=7,
        tokens_today=1200,
    )
    assert payload["team"] == "ml-platform"
    assert payload["model"] == "qwen"
    # Client cost/sensitive headers are untrusted and must not drive policy fields.
    assert payload["sensitive_data"] is False
    assert payload["forecast_monthly_cost_usd"] == 400.0
    assert payload["untrusted_context"]["sensitive_data"] is True
    assert payload["untrusted_context"]["forecast_monthly_cost_usd"] == "900"
    assert payload["requests_last_minute"] == 7
    assert payload["tokens_today"] == 1200
    assert payload["input_tokens"] >= 1
    assert payload["cost_per_request_usd"] > 0
    # Without a live spend counter, static config is used.
    assert payload["month_to_date_cost_usd"] == 100.0


def test_build_evaluate_payload_uses_live_spend_counter() -> None:
    request = httpx.Request("POST", "http://gw/v1/chat/completions")
    payload = build_evaluate_payload(
        request,
        {"model": "qwen", "messages": [{"content": "hi"}]},
        _governance_config(),
        {},
        month_to_date_cost_usd=42.5,
    )
    assert payload["month_to_date_cost_usd"] == 42.5
    assert payload["untrusted_context"]["month_to_date_cost_usd"] == ""


def test_build_evaluate_payload_includes_model_supply_chain_headers() -> None:
    request = httpx.Request(
        "POST",
        "http://gw/v1/chat/completions",
        headers={
            "x-ai-model-digest": "sha256:abc123",
            "x-ai-model-revision": "v1",
        },
    )
    payload = build_evaluate_payload(
        request,
        {"model": "llama3.1:8b", "messages": [{"content": "hello"}]},
        _governance_config(),
        {},
    )
    assert payload["model_artifact_digest"] == "sha256:abc123"
    assert payload["model_revision"] == "v1"


def test_build_evaluate_payload_prefers_server_model_registry() -> None:
    request = httpx.Request("POST", "http://gw/v1/chat/completions")
    target = ModelTarget(
        url="http://model",
        input_cost_per_million=1.0,
        output_cost_per_million=2.0,
        provider="vllm",
        model_revision="rev-9",
        model_artifact_digest="sha256:server",
    )
    payload = build_evaluate_payload(
        request,
        {"model": "qwen", "messages": [{"content": "hi"}]},
        _governance_config(),
        {"qwen": target},
    )
    assert payload["provider"] == "vllm"
    assert payload["model_revision"] == "rev-9"
    assert payload["model_artifact_digest"] == "sha256:server"


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
async def test_enforce_governance_forwards_model_supply_chain_headers() -> None:
    seen_digest: list[str] = []
    seen_revision: list[str] = []
    seen_body_digest: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/governance/evaluate":
            seen_digest.append(request.headers.get("x-ai-model-digest", ""))
            seen_revision.append(request.headers.get("x-ai-model-revision", ""))
            body = json.loads(request.content.decode())
            seen_body_digest.append(body.get("model_artifact_digest", ""))
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
                headers={
                    "x-ai-model-digest": "sha256:abc123",
                    "x-ai-model-revision": "v1",
                },
            ),
            {"model": "llama3.1:8b", "messages": []},
            {},
        )

    assert seen_digest == ["sha256:abc123"]
    assert seen_revision == ["v1"]
    assert seen_body_digest == ["sha256:abc123"]


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
                "approval_id": "apr_123",
                "decision_id": "dec_456",
                "policy_digest": "pol_789",
                "request_digest": "req_digest",
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
    detail = error.value.detail
    assert detail["approval_id"] == "apr_123"
    assert detail["decision_id"] == "dec_456"
    assert detail["policy_digest"] == "pol_789"
    assert detail["request_digest"] == "req_digest"
    assert detail["retry"]["header"] == "x-ai-approval-id"
    assert error.value.headers["x-ai-approval-id"] == "apr_123"
    assert error.value.headers["x-ai-decision-id"] == "dec_456"
    assert error.value.headers["x-ai-control-decision-id"] == "dec_456"
    assert error.value.headers["x-ai-policy-digest"] == "pol_789"
    assert error.value.headers["x-ai-request-digest"] == "req_digest"
    assert "x-ai-runtime-version" in error.value.headers


@pytest.mark.anyio
async def test_enforce_governance_forwards_approval_id() -> None:
    seen_approval: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/governance/evaluate":
            seen_approval.append(request.headers.get("x-ai-approval-id", ""))
            return httpx.Response(
                200,
                json={"final_verdict": "allow", "reasons": ["approved"], "stages": {}},
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
                headers={"x-ai-approval-id": "apr_123"},
            ),
            {"model": "qwen", "messages": []},
            {},
        )

    assert seen_approval == ["apr_123"]


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
    app.state.tenant_attribution = None
    app.state.decision_replay = None
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
    app.state.tenant_attribution = None
    app.state.decision_replay = None
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://gw") as client:
        response = await client.post("/v1/chat/completions", json={"model": "qwen", "messages": []})
    assert response.status_code == 403
    body = response.json()
    assert "detail" not in body
    assert body["final_verdict"] == "block"
    assert body["error"]["code"] == "governance_blocked"
    await upstream.aclose()


@pytest.mark.anyio
async def test_enforce_without_control_plane_url_fails_readiness() -> None:
    """Negative: missing CONTROL_PLANE_URL under enforce must not report ready."""
    upstream = _bootstrap_app(
        governance=_governance_config(control_plane_url="", mode="enforce", enabled=True)
    )
    ready, report = await build_readiness_report(app)
    assert ready is False
    assert report["checks"]["control_plane"]["ok"] is False
    assert report["checks"]["control_plane"]["required"] is True
    assert report["checks"]["control_plane"]["mode"] == "enforce"
    assert "CONTROL_PLANE_URL" in report["checks"]["control_plane_error"]
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://gw") as client:
        response = await client.get("/readyz")
    assert response.status_code == 503
    await upstream.aclose()


@pytest.mark.anyio
async def test_enforce_without_control_plane_url_rejects_chat_with_503() -> None:
    """Negative: enforce without CP must not silently skip governance."""
    upstream_calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        upstream_calls.append(request.url.path)
        return httpx.Response(200, json={"usage": {}}, request=request)

    upstream = _bootstrap_app(
        governance=_governance_config(control_plane_url="", mode="enforce", enabled=True),
        handler=handler,
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://gw") as client:
        response = await client.post(
            "/v1/chat/completions",
            json={"model": "qwen", "messages": [{"role": "user", "content": "hi"}]},
        )
    assert response.status_code == 503
    body = response.json()
    assert "detail" not in body
    assert body["error"]["code"] == "governance_misconfigured"
    assert upstream_calls == []
    await upstream.aclose()


@pytest.mark.anyio
async def test_disabled_mode_ready_and_health_exposes_mode() -> None:
    from app.gateway.governance import observe_governance_mode

    upstream = _bootstrap_app(
        governance=_governance_config(control_plane_url="", mode="disabled", enabled=False)
    )
    observe_governance_mode("disabled")
    ready, report = await build_readiness_report(app)
    assert ready is True
    assert report["checks"]["control_plane"]["mode"] == "disabled"
    assert report["checks"]["control_plane"]["required"] is False
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://gw") as client:
        health = await client.get("/health")
        healthz = await client.get("/healthz")
        metrics = await client.get("/metrics")
        chat = await client.post(
            "/v1/chat/completions",
            json={"model": "qwen", "messages": [{"role": "user", "content": "hi"}]},
        )
    assert health.status_code == 200
    assert health.json()["governance_mode"] == "disabled"
    assert healthz.json()["governance_mode"] == "disabled"
    assert chat.status_code == 200
    assert 'gateway_governance_mode{mode="disabled"} 1.0' in metrics.text
    assert 'gateway_governance_mode{mode="enforce"} 0.0' in metrics.text
    await upstream.aclose()


@pytest.mark.anyio
async def test_disabled_mode_does_not_call_control_plane() -> None:
    """Negative: disabled must not enforce even if a URL is present."""
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        return httpx.Response(200, json={"usage": {}}, request=request)

    upstream = _bootstrap_app(
        governance=_governance_config(
            control_plane_url="http://control-plane:8080",
            mode="disabled",
            enabled=False,
        ),
        handler=handler,
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://gw") as client:
        response = await client.post(
            "/v1/chat/completions",
            json={"model": "qwen", "messages": [{"role": "user", "content": "hi"}]},
        )
    assert response.status_code == 200
    assert "/governance/evaluate" not in calls
    await upstream.aclose()
