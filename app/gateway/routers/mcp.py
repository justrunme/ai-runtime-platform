"""MCP and intent proxy endpoints (reference maturity)."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

from app.gateway.governance import GovernanceConfig
from app.gateway.intent import resolve_intent
from app.gateway.mcp import enforce_tool_governance, governed_tool_response

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
    return JSONResponse(
        governed_tool_response(tool_name, payload, governance_result),
    )
