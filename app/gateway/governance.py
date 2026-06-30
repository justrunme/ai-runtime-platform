"""Runtime enforcement adapter for the AI Infrastructure Control Plane."""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

import httpx
from fastapi import HTTPException, Request
from prometheus_client import Counter

GOVERNANCE_DECISIONS = Counter(
    "gateway_governance_decisions_total",
    "Governance verdicts returned by the control plane before inference execution.",
    ["verdict", "team"],
)


@dataclass(frozen=True)
class GovernanceConfig:
    control_plane_url: str
    enabled: bool
    fail_open: bool
    timeout_seconds: float
    default_team: str
    default_owner: str
    default_environment: str
    default_namespace: str
    default_provider: str
    default_action: str
    default_cost_per_hour_usd: float
    default_month_to_date_cost_usd: float
    default_forecast_monthly_cost_usd: float

    @classmethod
    def from_environment(cls) -> GovernanceConfig | None:
        url = os.getenv("CONTROL_PLANE_URL", "").strip().rstrip("/")
        if not url:
            return None
        enabled = os.getenv("GOVERNANCE_ENFORCEMENT", "true").strip().lower() in {
            "1",
            "true",
            "yes",
        }
        fail_open = os.getenv("GOVERNANCE_FAIL_OPEN", "false").strip().lower() in {
            "1",
            "true",
            "yes",
        }
        return cls(
            control_plane_url=url,
            enabled=enabled,
            fail_open=fail_open,
            timeout_seconds=float(os.getenv("GOVERNANCE_TIMEOUT_SECONDS", "2.0")),
            default_team=os.getenv("GOVERNANCE_DEFAULT_TEAM", "platform"),
            default_owner=os.getenv("GOVERNANCE_DEFAULT_OWNER", "gateway"),
            default_environment=os.getenv("GOVERNANCE_DEFAULT_ENVIRONMENT", "development"),
            default_namespace=os.getenv("GOVERNANCE_DEFAULT_NAMESPACE", "ai-dev"),
            default_provider=os.getenv("GOVERNANCE_DEFAULT_PROVIDER", "ollama"),
            default_action=os.getenv("GOVERNANCE_DEFAULT_ACTION", "invoke_model"),
            default_cost_per_hour_usd=float(
                os.getenv("GOVERNANCE_DEFAULT_COST_PER_HOUR_USD", "0.18")
            ),
            default_month_to_date_cost_usd=float(
                os.getenv("GOVERNANCE_DEFAULT_MONTH_TO_DATE_COST_USD", "100")
            ),
            default_forecast_monthly_cost_usd=float(
                os.getenv("GOVERNANCE_DEFAULT_FORECAST_MONTHLY_COST_USD", "400")
            ),
        )


def _header_bool(request: Request, name: str) -> bool:
    value = request.headers.get(name, "").strip().lower()
    return value in {"1", "true", "yes"}


def _estimate_tokens(payload: dict[str, Any]) -> tuple[int, int]:
    messages = payload.get("messages")
    if not isinstance(messages, list):
        return 0, int(payload.get("max_tokens") or 0)

    prompt_chars = 0
    for message in messages:
        if not isinstance(message, dict):
            continue
        content = message.get("content")
        if isinstance(content, str):
            prompt_chars += len(content)
        elif isinstance(content, list):
            for part in content:
                if isinstance(part, dict) and isinstance(part.get("text"), str):
                    prompt_chars += len(part["text"])
    input_tokens = max(1, prompt_chars // 4)
    output_tokens = int(payload.get("max_tokens") or min(input_tokens, 512))
    return input_tokens, output_tokens


def build_evaluate_payload(
    request: Request,
    payload: dict[str, Any],
    config: GovernanceConfig,
    model_targets: dict[str, Any],
    *,
    requests_last_minute: int | None = None,
    tokens_today: int | None = None,
) -> dict[str, Any]:
    input_tokens, output_tokens = _estimate_tokens(payload)
    model = str(payload.get("model") or "unknown")
    target = model_targets.get(model)
    input_rate = getattr(target, "input_cost_per_million", 0.0) if target else 0.0
    output_rate = getattr(target, "output_cost_per_million", 0.0) if target else 0.0
    cost_per_request_usd = round(
        (input_tokens * input_rate + output_tokens * output_rate) / 1_000_000,
        6,
    )
    team = (
        request.headers.get("x-ai-team")
        or request.headers.get("x-ai-tenant")
        or config.default_team
    )

    return {
        "team": team,
        "owner": request.headers.get("x-ai-owner", config.default_owner),
        "environment": request.headers.get("x-ai-environment", config.default_environment),
        "namespace": request.headers.get("x-ai-namespace", config.default_namespace),
        "action": request.headers.get("x-ai-action", config.default_action),
        "model": model,
        "provider": request.headers.get("x-ai-provider", config.default_provider),
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cost_per_request_usd": cost_per_request_usd,
        "cost_per_hour_usd": float(
            request.headers.get("x-ai-cost-per-hour-usd", config.default_cost_per_hour_usd)
        ),
        "month_to_date_cost_usd": float(
            request.headers.get(
                "x-ai-month-to-date-cost-usd", config.default_month_to_date_cost_usd
            )
        ),
        "forecast_monthly_cost_usd": float(
            request.headers.get(
                "x-ai-forecast-monthly-cost-usd", config.default_forecast_monthly_cost_usd
            )
        ),
        "sensitive_data": _header_bool(request, "x-ai-sensitive-data"),
        "tool_access": _header_bool(request, "x-ai-tool-access"),
        "write_permission": _header_bool(request, "x-ai-write-permission"),
        "requests_last_minute": int(
            request.headers.get("x-ai-requests-last-minute", requests_last_minute or 0)
        ),
        "tokens_today": int(request.headers.get("x-ai-tokens-today", tokens_today or 0)),
    }


async def enforce_governance(
    client: httpx.AsyncClient,
    config: GovernanceConfig,
    request: Request,
    payload: dict[str, Any],
    model_targets: dict[str, Any],
    *,
    requests_last_minute: int | None = None,
    tokens_today: int | None = None,
) -> dict[str, Any] | None:
    """Call the control plane and reject the request when governance blocks it."""
    if not config.enabled:
        return None

    body = build_evaluate_payload(
        request,
        payload,
        config,
        model_targets,
        requests_last_minute=requests_last_minute,
        tokens_today=tokens_today,
    )
    evaluate_url = f"{config.control_plane_url}/governance/evaluate"

    try:
        response = await client.post(evaluate_url, json=body, timeout=config.timeout_seconds)
        response.raise_for_status()
        result = response.json()
    except httpx.HTTPError as error:
        GOVERNANCE_DECISIONS.labels(verdict="control_plane_error", team=body["team"]).inc()
        if config.fail_open:
            GOVERNANCE_DECISIONS.labels(verdict="fail_open", team=body["team"]).inc()
            return None
        raise HTTPException(
            status_code=503,
            detail={
                "error": "governance control plane unavailable",
                "control_plane_url": config.control_plane_url,
                "reason": str(error),
            },
        ) from error

    verdict = str(result.get("final_verdict", "unknown"))
    GOVERNANCE_DECISIONS.labels(verdict=verdict, team=body["team"]).inc()

    if verdict == "block":
        raise HTTPException(
            status_code=403,
            detail={
                "error": "governance blocked the request",
                "final_verdict": verdict,
                "reasons": result.get("reasons", []),
                "stages": result.get("stages", {}),
            },
        )
    if verdict == "approval_required":
        raise HTTPException(
            status_code=409,
            detail={
                "error": "governance approval required",
                "final_verdict": verdict,
                "reasons": result.get("reasons", []),
                "stages": result.get("stages", {}),
            },
        )
    return result
