"""Intent resolution proxy to the control plane."""

from __future__ import annotations

from typing import Any

import httpx
from fastapi import HTTPException, Request

from app.gateway.governance import GovernanceConfig
from app.gateway.identity import resolve_workload_identity


def build_intent_payload(
    request: Request, payload: dict[str, Any], config: GovernanceConfig
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
        "message": str(payload.get("message", "")).strip(),
        "subject": identity.subject,
        "groups": list(identity.groups),
        "policy_pack": identity.policy_pack,
        "team": identity.team,
        "owner": identity.owner,
        "environment": identity.environment,
        "namespace": identity.namespace,
        "region": request.headers.get("x-ai-region", "").strip(),
        "run_governance": bool(payload.get("run_governance", True)),
    }


async def resolve_intent(
    client: httpx.AsyncClient,
    config: GovernanceConfig,
    request: Request,
    payload: dict[str, Any],
) -> dict[str, Any]:
    body = build_intent_payload(request, payload, config)
    if not body["message"]:
        raise HTTPException(status_code=400, detail={"error": "message is required"})

    url = f"{config.control_plane_url}/intent/resolve"
    headers: dict[str, str] = {}
    authorization = request.headers.get("authorization", "").strip()
    if authorization:
        headers["authorization"] = authorization
    region = request.headers.get("x-ai-region", "").strip()
    if region:
        headers["x-ai-region"] = region
    team = request.headers.get("x-ai-team", "").strip()
    if team:
        headers["x-ai-team"] = team

    try:
        response = await client.post(
            url,
            json=body,
            headers=headers,
            timeout=config.timeout_seconds,
        )
        response.raise_for_status()
        return response.json()
    except httpx.HTTPError as error:
        raise HTTPException(
            status_code=503,
            detail={
                "error": "intent control plane unavailable",
                "control_plane_url": config.control_plane_url,
                "reason": str(error),
            },
        ) from error
