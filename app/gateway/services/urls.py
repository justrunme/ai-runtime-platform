"""URL and cost helpers for upstream model backends."""

from __future__ import annotations

from app.gateway.config import ModelTarget


def request_cost(usage: dict[str, int] | None, target: ModelTarget) -> float | None:
    """Return estimated USD cost from OpenAI usage data, or None when usage is absent."""
    if usage is None:
        return None
    input_tokens = usage.get("prompt_tokens", 0)
    output_tokens = usage.get("completion_tokens", 0)
    return round(
        (
            input_tokens * target.input_cost_per_million
            + output_tokens * target.output_cost_per_million
        )
        / 1_000_000,
        8,
    )


def chat_completions_url(base_url: str) -> str:
    """Build a chat-completions endpoint from an OpenAI-compatible base URL."""
    normalized = base_url.rstrip("/")
    suffix = "/chat/completions" if normalized.endswith("/v1") else "/v1/chat/completions"
    return f"{normalized}{suffix}"


def backend_health_url(target: ModelTarget) -> str:
    """Build the health endpoint without assuming the OpenAI API base path."""
    base_url = target.url.rstrip("/")
    if base_url.endswith("/v1"):
        base_url = base_url.removesuffix("/v1")
    return f"{base_url}{target.health_path}"
