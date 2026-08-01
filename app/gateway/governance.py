"""Runtime enforcement adapter for the AI Infrastructure Control Plane."""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

import httpx
from fastapi import HTTPException, Request
from prometheus_client import Counter

from app.gateway.identity import (
    claim_flag,
    is_trusted_proxy_enabled,
    resolve_workload_identity,
)

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


def _extract_prompt_text(payload: dict[str, Any]) -> str:
    messages = payload.get("messages")
    if not isinstance(messages, list):
        return str(payload.get("prompt_text") or "")

    parts: list[str] = []
    for message in messages:
        if not isinstance(message, dict):
            continue
        content = message.get("content")
        if isinstance(content, str):
            parts.append(content)
        elif isinstance(content, list):
            for item in content:
                if isinstance(item, dict) and isinstance(item.get("text"), str):
                    parts.append(item["text"])
    return "\n".join(parts)


def _client_hint(request: Request, name: str) -> str:
    return request.headers.get(name, "").strip()


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
    identity = resolve_workload_identity(
        request,
        {
            "team": config.default_team,
            "owner": config.default_owner,
            "environment": config.default_environment,
            "namespace": config.default_namespace,
        },
    )

    provider = getattr(target, "provider", None) if target else None
    if not provider:
        provider = config.default_provider

    model_revision = getattr(target, "model_revision", None) if target else None
    model_digest = getattr(target, "model_artifact_digest", None) if target else None
    if is_trusted_proxy_enabled():
        model_revision = model_revision or _client_hint(request, "x-ai-model-revision")
        model_digest = model_digest or _client_hint(request, "x-ai-model-digest")

    sensitive_data = claim_flag(identity, "sensitive_data", "ai_sensitive_data")
    tool_access = claim_flag(identity, "tool_access", "ai_tool_access")
    write_permission = claim_flag(identity, "write_permission", "ai_write_permission")

    untrusted_context = {
        "team": _client_hint(request, "x-ai-team") or _client_hint(request, "x-ai-tenant"),
        "owner": _client_hint(request, "x-ai-owner"),
        "subject": _client_hint(request, "x-ai-subject"),
        "sensitive_data": _header_bool(request, "x-ai-sensitive-data"),
        "tool_access": _header_bool(request, "x-ai-tool-access"),
        "write_permission": _header_bool(request, "x-ai-write-permission"),
        "cost_per_hour_usd": _client_hint(request, "x-ai-cost-per-hour-usd"),
        "month_to_date_cost_usd": _client_hint(request, "x-ai-month-to-date-cost-usd"),
        "forecast_monthly_cost_usd": _client_hint(request, "x-ai-forecast-monthly-cost-usd"),
        "requests_last_minute": _client_hint(request, "x-ai-requests-last-minute"),
        "tokens_today": _client_hint(request, "x-ai-tokens-today"),
        "provider": _client_hint(request, "x-ai-provider"),
        "model_revision": _client_hint(request, "x-ai-model-revision"),
        "model_artifact_digest": _client_hint(request, "x-ai-model-digest"),
        "identity_source": identity.source,
        "trusted_proxy": is_trusted_proxy_enabled(),
    }

    return {
        "subject": identity.subject,
        "groups": list(identity.groups),
        "policy_pack": identity.policy_pack,
        "team": identity.team,
        "owner": identity.owner,
        "environment": identity.environment,
        "namespace": identity.namespace,
        "action": config.default_action,
        "model": model,
        "provider": str(provider),
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cost_per_request_usd": cost_per_request_usd,
        # Server-derived cost/quota signals — never accept client overrides for policy.
        "cost_per_hour_usd": float(config.default_cost_per_hour_usd),
        "month_to_date_cost_usd": float(config.default_month_to_date_cost_usd),
        "forecast_monthly_cost_usd": float(config.default_forecast_monthly_cost_usd),
        "sensitive_data": sensitive_data,
        "tool_access": tool_access,
        "write_permission": write_permission,
        "requests_last_minute": int(requests_last_minute or 0),
        "tokens_today": int(tokens_today or 0),
        "model_revision": str(model_revision or ""),
        "model_artifact_digest": str(model_digest or ""),
        "prompt_text": _extract_prompt_text(payload),
        "agent": str(identity.claims.get("agent") or "")
        if identity.claims
        else (_client_hint(request, "x-ai-agent") if is_trusted_proxy_enabled() else ""),
        "region": str(identity.claims.get("region") or "")
        if identity.claims
        else (_client_hint(request, "x-ai-region") if is_trusted_proxy_enabled() else ""),
        "untrusted_context": untrusted_context,
    }


def governance_detail(
    *,
    code: str,
    message: str,
    result: dict[str, Any],
    control_plane_url: str | None = None,
    reason: str | None = None,
) -> dict[str, Any]:
    detail: dict[str, Any] = {
        "error": {
            "type": "governance_error",
            "code": code,
            "message": message,
        },
        "final_verdict": result.get("final_verdict"),
        "reasons": result.get("reasons", []),
        "stages": result.get("stages", {}),
        "decision_id": result.get("decision_id"),
        "approval_id": result.get("approval_id"),
        "policy_bundle_id": result.get("policy_bundle_id"),
        "policy_digest": result.get("policy_digest"),
        "request_digest": result.get("request_digest"),
        "retry": None,
    }
    if detail["approval_id"]:
        detail["retry"] = {
            "instruction": (
                "Approve the request at the control plane, then retry this gateway "
                "request with header x-ai-approval-id set to approval_id."
            ),
            "approve_path": f"/approvals/{detail['approval_id']}/approve",
            "header": "x-ai-approval-id",
            "approval_id": detail["approval_id"],
        }
    if control_plane_url is not None:
        detail["control_plane_url"] = control_plane_url
    if reason is not None:
        detail["reason"] = reason
    return detail


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
    headers: dict[str, str] = {}
    request_id = request.headers.get("x-request-id")
    if request_id:
        headers["x-request-id"] = request_id
    authorization = request.headers.get("authorization", "").strip()
    if authorization:
        headers["authorization"] = authorization
    approval_id = request.headers.get("x-ai-approval-id", "").strip()
    if approval_id:
        headers["x-ai-approval-id"] = approval_id
    model_digest = str(body.get("model_artifact_digest") or "").strip()
    if model_digest:
        headers["x-ai-model-digest"] = model_digest
    model_revision = str(body.get("model_revision") or "").strip()
    if model_revision:
        headers["x-ai-model-revision"] = model_revision

    try:
        response = await client.post(
            evaluate_url,
            json=body,
            headers=headers,
            timeout=config.timeout_seconds,
        )
        response.raise_for_status()
        result = response.json()
    except httpx.HTTPError as error:
        GOVERNANCE_DECISIONS.labels(verdict="control_plane_error", team=body["team"]).inc()
        if config.fail_open:
            GOVERNANCE_DECISIONS.labels(verdict="fail_open", team=body["team"]).inc()
            return None
        raise HTTPException(
            status_code=503,
            detail=governance_detail(
                code="control_plane_unavailable",
                message="governance control plane unavailable",
                result={"final_verdict": "control_plane_error", "reasons": [], "stages": {}},
                control_plane_url=config.control_plane_url,
                reason=str(error),
            ),
        ) from error

    from app.gateway.decision_token import bind_signed_decision
    from app.gateway.evidence import apply_evidence_headers, evidence_from_governance

    result = await bind_signed_decision(result)
    verdict = str(result.get("final_verdict", "unknown"))
    GOVERNANCE_DECISIONS.labels(verdict=verdict, team=body["team"]).inc()

    if verdict == "block":
        raise HTTPException(
            status_code=403,
            detail=governance_detail(
                code="governance_blocked",
                message="governance blocked the request",
                result=result,
            ),
            headers=apply_evidence_headers(
                {},
                evidence_from_governance(result, enforcement_outcome="blocked"),
            )
            or None,
        )
    if verdict == "approval_required":
        response_headers = apply_evidence_headers(
            {},
            evidence_from_governance(result, enforcement_outcome="approval_required"),
        )
        raise HTTPException(
            status_code=409,
            detail=governance_detail(
                code="governance_approval_required",
                message="governance approval required",
                result=result,
            ),
            headers=response_headers or None,
        )
    return result
