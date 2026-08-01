"""MCP and intent proxy endpoints."""

from __future__ import annotations

from contextlib import suppress
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

from app.gateway.governance import GovernanceConfig
from app.gateway.intent import resolve_intent
from app.gateway.mcp import enforce_tool_governance, governed_tool_response
from app.gateway.mcp_transport import execute_mcp_tool, load_mcp_registry

router = APIRouter(tags=["mcp"])


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
async def mcp_servers() -> dict[str, Any]:
    registry = load_mcp_registry()
    return {
        "servers": {
            name: {
                "url": cfg.url,
                "transport": cfg.transport,
                "credential_ref": cfg.credential_ref,
                "timeout_seconds": cfg.timeout_seconds,
                "max_result_bytes": cfg.max_result_bytes,
            }
            for name, cfg in registry.servers.items()
        }
    }


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
    if governance is not None:
        governance_result = await enforce_tool_governance(
            request.app.state.client,
            governance,
            request,
            tool_name,
            payload,
        )

    server_name = str(payload.get("mcp_server") or payload.get("server") or "").strip()
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
        # Optional audit sink — Control Plane may ignore unknown paths.
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
