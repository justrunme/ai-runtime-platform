import httpx
import pytest

from app.cli.replay import fetch_decision, main
from app.gateway.decisions import (
    DecisionRecord,
    InMemoryDecisionStore,
    format_decision_tree,
)
from app.gateway.main import (
    BackendHealthStore,
    GatewaySettings,
    ModelRoute,
    ModelTarget,
    RouteTarget,
    app,
    create_decision_store,
    resolve_shadow_backend,
    run_shadow_request,
)


def test_resolve_shadow_backend_skips_when_already_on_canary() -> None:
    route = ModelRoute(
        targets=[
            RouteTarget(model="qwen2.5:1.5b", weight=90),
            RouteTarget(model="llama3.2:1b", weight=10),
        ],
        shadow="llama3.2:1b",
    )
    assert resolve_shadow_backend(route, "qwen2.5:1.5b") == "llama3.2:1b"
    assert resolve_shadow_backend(route, "llama3.2:1b") is None


def test_route_rejects_shadow_outside_route_models() -> None:
    with pytest.raises(ValueError, match="shadow must reference"):
        ModelRoute(
            targets=[RouteTarget(model="qwen2.5:1.5b", weight=100)],
            shadow="missing",
        )


@pytest.mark.anyio
async def test_run_shadow_request_records_metrics_without_raising() -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.headers.get("x-shadow-traffic", ""))
        return httpx.Response(200, json={"usage": {}}, request=request)

    target = ModelTarget(url="http://shadow", input_cost_per_million=0, output_cost_per_million=0)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        outcome, duration_ms = await run_shadow_request(
            client, {"messages": []}, {"x-request-id": "shadow-1"}, "llama", target, 5.0
        )
    assert outcome == "success"
    assert duration_ms >= 0
    assert calls == ["true"]


@pytest.mark.anyio
async def test_decision_store_records_and_patches_shadow() -> None:
    store = InMemoryDecisionStore()
    await store.put(
        DecisionRecord(
            request_id="req-1",
            requested_model="small-chat-canary",
            selected_backend="qwen2.5:1.5b",
            routing_reason="primary",
            fallback_used=False,
            health_score=95,
            duration_ms=120.0,
            shadow_backend="llama3.2:1b",
        )
    )
    await store.patch_shadow("req-1", outcome="success", duration_ms=88.0)
    record = await store.get("req-1")
    assert record is not None
    assert record.shadow_outcome == "success"
    assert record.shadow_duration_ms == 88.0


def test_format_decision_tree_includes_shadow_line() -> None:
    tree = format_decision_tree(
        {
            "request_id": "req-1",
            "requested_model": "small-chat-canary",
            "selected_backend": "qwen2.5:1.5b",
            "routing_reason": "primary",
            "fallback_used": False,
            "health_score": 95,
            "duration_ms": 120.0,
            "shadow_backend": "llama3.2:1b",
            "shadow_outcome": "success",
            "shadow_duration_ms": 88.0,
        }
    )
    assert "shadow:   llama3.2:1b (success, 88.0 ms)" in tree


@pytest.mark.anyio
async def test_decision_endpoint_returns_recorded_routing() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"usage": {}}, request=request)

    settings = GatewaySettings(
        model_targets={
            "qwen": ModelTarget(
                url="http://primary", input_cost_per_million=0, output_cost_per_million=0
            )
        }
    )
    upstream = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    app.state.settings = settings
    app.state.client = upstream
    app.state.backend_health = BackendHealthStore(settings, upstream)
    app.state.decision_store = create_decision_store(None)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://gw") as client:
        await client.post(
            "/v1/chat/completions",
            json={"model": "qwen", "messages": []},
            headers={"x-request-id": "replay-demo-001"},
        )
        decision = await client.get("/v1/decisions/replay-demo-001")
    assert decision.status_code == 200
    body = decision.json()
    assert body["selected_backend"] == "qwen"
    assert body["request_id"] == "replay-demo-001"
    await upstream.aclose()


def test_cli_replay_prints_tree(monkeypatch, capsys) -> None:
    sample = {
        "request_id": "replay-demo-001",
        "requested_model": "small-chat",
        "selected_backend": "llama3.2:1b",
        "routing_reason": "fallback",
        "fallback_used": True,
        "health_score": 80,
        "duration_ms": 150.0,
        "shadow_backend": None,
        "shadow_outcome": None,
        "shadow_duration_ms": None,
        "estimated_cost": None,
        "recorded_at": 1.0,
    }
    monkeypatch.setattr("app.cli.replay.fetch_decision", lambda _url, _rid: sample)
    main(["replay", "--request-id", "replay-demo-001"])
    output = capsys.readouterr().out
    assert "replay-demo-001" in output
    assert "fallback" in output


def test_fetch_decision_exits_on_404(monkeypatch) -> None:
    import urllib.error

    def raise_404(*_args, **_kwargs):
        raise urllib.error.HTTPError("http://gw", 404, "missing", hdrs=None, fp=None)

    monkeypatch.setattr("urllib.request.urlopen", raise_404)
    with pytest.raises(SystemExit, match="not found"):
        fetch_decision("http://gw", "missing")
