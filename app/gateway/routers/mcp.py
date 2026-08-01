"""MCP and intent proxy endpoints."""

from __future__ import annotations

import os
from contextlib import suppress
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

from app.gateway.governance import GovernanceConfig
from app.gateway.intent import resolve_intent
from app.gateway.mcp import enforce_tool_governance, governed_tool_response
from app.gateway.mcp_transport import (
    cancel_mcp_request,
    execute_mcp_tool,
    initialize_mcp_session,
    load_mcp_registry,
    session_store,
)
from app.gateway.rbac import MCP_SERVERS_ROLES, require_any_role

router = APIRouter(tags=["mcp"])


def _allow_ungoverned_mcp() -> bool:
    return os.getenv("MCP_ALLOW_UNGOVERNED", "").strip().lower() in {"1", "true", "yes"}


@router.get("/mcp/tools")
async def mcp_tools(request: Request) -> JSONResponse:
    governance: GovernanceConfig | None = request.app.state.governance
    if governance is None:
        raise HTTPException(
            status_code=503,
            detail={"error": "CONTROL_PLANE_URL is required for MCP tool catalog"},
        )
    response = await request.app.state.client.get(
        f"{governance.control_plane_url}/registry/tools",
        timeout=governance.timeout_seconds,
    )
    response.raise_for_status()
    return JSONResponse(response.json())


@router.get("/mcp/servers")
async def mcp_servers(request: Request) -> dict[str, Any]:
    require_any_role(request, MCP_SERVERS_ROLES)
    registry = load_mcp_registry()
    return {
        "servers": {
            name: {
                "url": cfg.url,
                "transport": cfg.transport,
                "credential_ref": cfg.credential_ref,
                "timeout_seconds": cfg.timeout_seconds,
                "max_result_bytes": cfg.max_result_bytes,
                "session_mode": cfg.session_mode,
                "capability_digest": cfg.capability_digest,
                "active_session_id": (
                    session.session_id
                    if (session := session_store().get(name)) is not None
                    else None
                ),
            }
            for name, cfg in registry.servers.items()
        }
    }


@router.post("/mcp/servers/{server_name}/initialize")
async def mcp_server_initialize(server_name: str, request: Request) -> dict[str, Any]:
    require_any_role(request, MCP_SERVERS_ROLES)
    session = await initialize_mcp_session(
        request.app.state.client,
        server_name=server_name,
        force=True,
    )
    return {
        "server": server_name,
        "session_id": session.session_id,
        "protocol_version": session.protocol_version,
        "capability_digest": session.capability_digest,
        "capabilities": session.capabilities,
    }


@router.post("/mcp/servers/{server_name}/cancel")
async def mcp_server_cancel(server_name: str, request: Request) -> dict[str, Any]:
    require_any_role(request, MCP_SERVERS_ROLES)
    payload = await request.json()
    if not isinstance(payload, dict) or not payload.get("request_id"):
        raise HTTPException(
            status_code=422,
            detail={
                "error": {
                    "type": "invalid_request_error",
                    "code": "mcp_request_id_required",
                    "message": "body.request_id is required",
                }
            },
        )
    return await cancel_mcp_request(
        request.app.state.client,
        server_name=server_name,
        request_id=str(payload["request_id"]),
    )


@router.post("/v1/intent/resolve")
async def intent_resolve(request: Request) -> JSONResponse:
    payload = await request.json()
    governance: GovernanceConfig | None = request.app.state.governance
    if governance is None:
        raise HTTPException(
            status_code=503,
            detail={"error": "CONTROL_PLANE_URL is required for intent resolution"},
        )
    result = await resolve_intent(
        request.app.state.client,
        governance,
        request,
        payload,
    )
    return JSONResponse(result)


@router.post("/mcp/tools/{tool_name}/call")
async def mcp_tool_call(tool_name: str, request: Request) -> JSONResponse:
    payload = await request.json()
    if not isinstance(payload, dict):
        raise HTTPException(status_code=422, detail={"error": "request body must be an object"})
    governance: GovernanceConfig | None = request.app.state.governance
    governance_result = None
    server_name = str(payload.get("mcp_server") or payload.get("server") or "").strip()
    if server_name and governance is None and not _allow_ungoverned_mcp():
        raise HTTPException(
            status_code=503,
            detail={
                "error": {
                    "type": "api_error",
                    "code": "mcp_governance_required",
                    "message": (
                        "MCP execution requires CONTROL_PLANE_URL; "
                        "set MCP_ALLOW_UNGOVERNED=true only for local demos"
                    ),
                }
            },
        )
    if governance is not None:
        governance_result = await enforce_tool_governance(
            request.app.state.client,
            governance,
            request,
            tool_name,
            payload,
        )

    execution = None
    if server_name:
        arguments = payload.get("arguments")
        if not isinstance(arguments, dict):
            arguments = {}
        execution = await execute_mcp_tool(
            request.app.state.client,
            server_name=server_name,
            tool_name=tool_name,
            arguments=arguments,
        )
        # Audit sink — Control Plane may ignore unknown paths.
        if governance is not None:
            with suppress(Exception):
                await request.app.state.client.post(
                    f"{governance.control_plane_url}/governance/audit-tool",
                    json={
                        "tool": tool_name,
                        "server": server_name,
                        "verdict": (governance_result or {}).get("final_verdict", "allow"),
                        "truncated": execution.get("truncated", False),
                        "decision_id": (governance_result or {}).get("decision_id"),
                        "policy_digest": (governance_result or {}).get("policy_digest"),
                        "request_digest": (governance_result or {}).get("request_digest"),
                        "mcp_request_id": execution.get("mcp_request_id"),
                        "session_id": execution.get("session_id"),
                        "capability_digest": execution.get("capability_digest"),
                        "credential_ref": execution.get("credential_ref"),
                        "transport": execution.get("transport"),
                    },
                    timeout=min(2.0, governance.timeout_seconds),
                )

    return JSONResponse(
        governed_tool_response(
            tool_name,
            payload,
            governance_result,
            execution=execution,
        ),
    )
