import pytest
import httpx

from app.gateway.main import (
    GatewaySettings,
    BackendHealthStore,
    ModelRoute,
    ModelTarget,
    NoHealthyBackendError,
    RouteTarget,
    RoutingPolicy,
    RoutingWeights,
    chat_completions_url,
    post_completion_with_fallback,
    request_cost,
    resolve_route,
    select_route_target,
    select_health_aware_backend,
    select_cost_aware_backend,
)


def test_request_cost_uses_openai_usage_fields() -> None:
    target = ModelTarget(url="http://example.test", input_cost_per_million=0.25, output_cost_per_million=1.0)
    assert request_cost({"prompt_tokens": 2_000, "completion_tokens": 500}, target) == 0.001


def test_request_cost_returns_none_without_usage() -> None:
    target = ModelTarget(url="http://example.test", input_cost_per_million=0.25, output_cost_per_million=1.0)
    assert request_cost(None, target) is None


def test_model_targets_accept_gitops_json_environment(monkeypatch) -> None:
    monkeypatch.setenv(
        "MODEL_TARGETS",
        '{"small":{"url":"http://model","input_cost_per_million":0.1,"output_cost_per_million":0.2}}',
    )
    assert GatewaySettings.from_environment().model_targets["small"].url == "http://model"


def test_ollama_target_is_added_from_environment(monkeypatch) -> None:
    monkeypatch.setenv("OLLAMA_BASE_URL", "http://ollama:11434")
    monkeypatch.setenv("OLLAMA_MODEL", "qwen2.5:1.5b")
    target = GatewaySettings.from_environment().model_targets["qwen2.5:1.5b"]
    assert target.url == "http://ollama:11434/v1"
    assert target.input_cost_per_million == 0


def test_ollama_base_url_is_not_given_a_second_v1_suffix(monkeypatch) -> None:
    monkeypatch.setenv("OLLAMA_BASE_URL", "http://ollama:11434/v1/")
    target = GatewaySettings.from_environment().model_targets["qwen2.5:1.5b"]
    assert target.url == "http://ollama:11434/v1"


def test_chat_completions_url_handles_origin_and_v1_base() -> None:
    assert chat_completions_url("http://vllm:8000") == "http://vllm:8000/v1/chat/completions"
    assert chat_completions_url("http://ollama:11434/v1/") == "http://ollama:11434/v1/chat/completions"


def test_canary_route_selection_is_stable_for_a_request() -> None:
    route = ModelRoute(
        targets=[RouteTarget(model="qwen2.5:1.5b", weight=90), RouteTarget(model="llama3.2:1b", weight=10)]
    )
    assert select_route_target(route, "request-42", "small-chat") == select_route_target(
        route, "request-42", "small-chat"
    )


def test_route_rejects_weights_that_do_not_total_100() -> None:
    with pytest.raises(ValueError, match="total 100"):
        ModelRoute(targets=[RouteTarget(model="qwen2.5:1.5b", weight=99)])


def test_settings_reject_route_model_that_has_no_target(monkeypatch) -> None:
    monkeypatch.setenv(
        "MODEL_ROUTES", '{"small-chat":{"targets":[{"model":"missing","weight":100}]}}'
    )
    with pytest.raises(ValueError, match="unknown models"):
        GatewaySettings.from_environment()


def test_failover_route_resolves_primary_and_fallback() -> None:
    route = ModelRoute(primary="qwen2.5:1.5b", fallback="llama3.2:1b")
    assert resolve_route(route, "request-42", "small-chat") == ("qwen2.5:1.5b", "llama3.2:1b")


@pytest.mark.anyio
async def test_healthy_primary_does_not_use_fallback() -> None:
    requests: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(str(request.url))
        return httpx.Response(200, json={"usage": {}}, request=request)

    primary = ModelTarget(url="http://primary", input_cost_per_million=0, output_cost_per_million=0)
    fallback = ModelTarget(url="http://fallback", input_cost_per_million=0, output_cost_per_million=0)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        _, selected, fallback_used, failed_models = await post_completion_with_fallback(
            client, {"messages": []}, {}, "qwen", primary, "llama", fallback
        )
    assert (selected, fallback_used) == ("qwen", False)
    assert failed_models == []
    assert requests == ["http://primary/v1/chat/completions"]


@pytest.mark.anyio
async def test_timeout_retries_fallback_model() -> None:
    requests: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request.url.host or "")
        if request.url.host == "primary":
            raise httpx.ReadTimeout("primary timed out", request=request)
        return httpx.Response(200, json={"usage": {}}, request=request)

    primary = ModelTarget(url="http://primary", input_cost_per_million=0, output_cost_per_million=0)
    fallback = ModelTarget(url="http://fallback", input_cost_per_million=0, output_cost_per_million=0)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        _, selected, fallback_used, failed_models = await post_completion_with_fallback(
            client, {"messages": []}, {}, "qwen", primary, "llama", fallback
        )
    assert (selected, fallback_used) == ("llama", True)
    assert failed_models == ["qwen"]
    assert requests == ["primary", "fallback"]


@pytest.mark.anyio
async def test_server_error_retries_fallback_model() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "primary":
            return httpx.Response(503, request=request)
        return httpx.Response(200, json={"usage": {}}, request=request)

    primary = ModelTarget(url="http://primary", input_cost_per_million=0, output_cost_per_million=0)
    fallback = ModelTarget(url="http://fallback", input_cost_per_million=0, output_cost_per_million=0)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        _, selected, fallback_used, failed_models = await post_completion_with_fallback(
            client, {"messages": []}, {}, "qwen", primary, "llama", fallback
        )
    assert (selected, fallback_used) == ("llama", True)
    assert failed_models == ["qwen"]


@pytest.mark.anyio
async def test_backend_health_snapshot_uses_probes_and_request_signals() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, request=request)

    settings = GatewaySettings(
        model_targets={
            "qwen2.5:1.5b": ModelTarget(
                url="http://primary/v1",
                backend_name="qwen-local",
                input_cost_per_million=0,
                output_cost_per_million=0,
            )
        }
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        health = BackendHealthStore(settings, client)
        await health.probe_all()
        await health.record_request("qwen2.5:1.5b", success=True)
        await health.record_request("qwen2.5:1.5b", success=False, fallback_used=True)
        snapshot = await health.snapshot()

    assert snapshot[0]["name"] == "qwen-local"
    assert snapshot[0]["status"] == "healthy"
    assert snapshot[0]["error_rate"] == 0.5
    assert snapshot[0]["fallback_rate"] == 0.5
    assert snapshot[0]["score"] < 100


def health_aware_route() -> ModelRoute:
    return ModelRoute(primary="qwen", fallback="llama", min_health_score=50, unhealthy_action="skip")


def health_settings() -> GatewaySettings:
    return GatewaySettings(
        model_targets={
            "qwen": ModelTarget(url="http://primary", input_cost_per_million=0, output_cost_per_million=0),
            "llama": ModelTarget(url="http://fallback", input_cost_per_million=0, output_cost_per_million=0),
        }
    )


@pytest.mark.anyio
async def test_health_aware_route_keeps_healthy_primary() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        health = BackendHealthStore(health_settings(), client)
        await health.probe_all()
        assert await select_health_aware_backend(health_aware_route(), "qwen", "llama", health) == ("qwen", False)


@pytest.mark.anyio
async def test_health_aware_route_skips_low_score_primary() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503 if request.url.host == "primary" else 200, request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        health = BackendHealthStore(health_settings(), client)
        await health.probe_all()
        assert await select_health_aware_backend(health_aware_route(), "qwen", "llama", health) == ("llama", True)


@pytest.mark.anyio
async def test_health_aware_route_returns_no_backend_when_all_scores_are_low() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        health = BackendHealthStore(health_settings(), client)
        await health.probe_all()
        with pytest.raises(NoHealthyBackendError):
            await select_health_aware_backend(health_aware_route(), "qwen", "llama", health)


@pytest.mark.anyio
async def test_cost_aware_route_selects_cheaper_healthy_backend() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, request=request)

    settings = GatewaySettings(
        model_targets={
            "qwen": ModelTarget(url="http://primary", input_cost_per_million=1, output_cost_per_million=1),
            "llama": ModelTarget(url="http://fallback", input_cost_per_million=0.1, output_cost_per_million=0.1),
        }
    )
    route = ModelRoute(
        primary="qwen",
        fallback="llama",
        min_health_score=50,
        routing_policy=RoutingPolicy(weights=RoutingWeights(health=0.5, latency=0.3, cost=0.2)),
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        health = BackendHealthStore(settings, client)
        await health.probe_all()
        assert await select_cost_aware_backend(route, "qwen", "llama", health, settings.model_targets) == ("llama", True)
