from app.gateway.main import GatewaySettings, ModelTarget, request_cost


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
