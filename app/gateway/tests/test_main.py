import pytest

from app.gateway.main import (
    GatewaySettings,
    ModelRoute,
    ModelTarget,
    RouteTarget,
    chat_completions_url,
    request_cost,
    select_route_target,
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
