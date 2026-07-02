"""MCP gateway adapter with governed tool calls."""

from __future__ import annotations

from typing import Any

import httpx
from fastapi import HTTPException, Request
from prometheus_client import Counter

from app.gateway.governance import GovernanceConfig
from app.gateway.identity import resolve_workload_identity

MCP_TOOL_CALLS = Counter(
    "gateway_mcp_tool_calls_total",
    "Governed MCP tool calls handled by the execution plane gateway.",
    ["verdict", "tool", "team"],
)


def build_tool_evaluate_payload(
    request: Request,
    tool_name: str,
    payload: dict[str, Any],
    config: GovernanceConfig,
) -> dict[str, Any]:
    identity = resolve_workload_identity(
        request,
        {
            "team": config.default_team,
            "owner": config.default_owner,
            "environment": config.default_environment,
            "namespace": config.default_namespace,
        },
    )
    return {
        "subject": identity.subject,
        "groups": list(identity.groups),
        "policy_pack": identity.policy_pack,
        "team": identity.team,
        "owner": identity.owner,
        "environment": identity.environment,
        "namespace": identity.namespace,
        "agent": request.headers.get("x-ai-agent", "").strip(),
        "tool": tool_name,
        "action": str(payload.get("action") or "invoke"),
        "mcp_server": str(payload.get("mcp_server") or ""),
        "write_permission": str(payload.get("write_permission", "")).lower() in {"1", "true", "yes"}
        or request.headers.get("x-ai-write-permission", "").strip().lower() in {"1", "true", "yes"},
    }


async def enforce_tool_governance(
    client: httpx.AsyncClient,
    config: GovernanceConfig,
    request: Request,
    tool_name: str,
    payload: dict[str, Any],
) -> dict[str, Any] | None:
    if not config.enabled:
        return None

    body = build_tool_evaluate_payload(request, tool_name, payload, config)
    evaluate_url = f"{config.control_plane_url}/governance/evaluate-tool"
    headers: dict[str, str] = {}
    request_id = request.headers.get("x-request-id")
    if request_id:
        headers["x-request-id"] = request_id
    authorization = request.headers.get("authorization", "").strip()
    if authorization:
        headers["authorization"] = authorization
    agent = request.headers.get("x-ai-agent", "").strip()
    if agent:
        headers["x-ai-agent"] = agent

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
        MCP_TOOL_CALLS.labels(
            verdict="control_plane_error", tool=tool_name, team=body["team"]
        ).inc()
        if config.fail_open:
            MCP_TOOL_CALLS.labels(verdict="fail_open", tool=tool_name, team=body["team"]).inc()
            return None
        raise HTTPException(
            status_code=503,
            detail={
                "error": "tool governance control plane unavailable",
                "control_plane_url": config.control_plane_url,
                "reason": str(error),
            },
        ) from error

    verdict = str(result.get("final_verdict", "unknown"))
    MCP_TOOL_CALLS.labels(verdict=verdict, tool=tool_name, team=body["team"]).inc()

    if verdict == "block":
        raise HTTPException(
            status_code=403,
            detail={
                "error": "tool governance blocked the request",
                "final_verdict": verdict,
                "reasons": result.get("reasons", []),
                "stages": result.get("stages", {}),
            },
        )
    return result


def governed_tool_response(
    tool_name: str,
    payload: dict[str, Any],
    governance_result: dict[str, Any] | None,
) -> dict[str, Any]:
    return {
        "tool": tool_name,
        "action": payload.get("action", "invoke"),
        "status": "governed_stub",
        "message": "tool call allowed by control plane; connect MCP backend in production",
        "arguments": payload.get("arguments", {}),
        "governance": governance_result or {"final_verdict": "skipped"},
    }
