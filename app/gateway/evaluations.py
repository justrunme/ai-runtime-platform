"""Post-response evaluation submission to the control plane."""

from __future__ import annotations

import os
from typing import Any

import httpx

from app.gateway.governance import GovernanceConfig, _extract_prompt_text


def response_evaluation_enabled() -> bool:
    return os.getenv("GOVERNANCE_EVALUATE_RESPONSE", "false").strip().lower() in {
        "1",
        "true",
        "yes",
    }


def build_evaluation_payload(
    *,
    team: str,
    model: str,
    request_id: str,
    chat_payload: dict[str, Any],
    completion: dict[str, Any],
    latency_ms: float,
    cost_usd: float | None,
) -> dict[str, Any]:
    response_text = ""
    choices = completion.get("choices")
    if isinstance(choices, list) and choices:
        message = choices[0].get("message", {})
        if isinstance(message, dict) and isinstance(message.get("content"), str):
            response_text = message["content"]

    return {
        "team": team,
        "model": model,
        "request_id": request_id,
        "prompt_text": _extract_prompt_text(chat_payload),
        "response_text": response_text,
        "latency_ms": round(latency_ms, 2),
        "cost_usd": float(cost_usd or 0.0),
    }


async def submit_response_evaluation(
    client: httpx.AsyncClient,
    config: GovernanceConfig,
    payload: dict[str, Any],
) -> None:
    url = f"{config.control_plane_url}/governance/evaluate-response"
    try:
        await client.post(url, json=payload, timeout=config.timeout_seconds)
    except httpx.HTTPError:
        return
