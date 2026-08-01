"""MCP execution transport (Streamable HTTP) with credential references."""

from __future__ import annotations

import json
import os
from functools import lru_cache
from typing import Any

import httpx
from fastapi import HTTPException
from pydantic import BaseModel, Field


class McpServerConfig(BaseModel):
    url: str
    transport: str = "http"  # http | sse (sse reserved)
    credential_ref: str | None = Field(default=None, alias="credentialRef")
    timeout_seconds: float = Field(default=30.0, gt=0, alias="timeoutSeconds")
    max_result_bytes: int = Field(default=1_048_576, ge=64, alias="maxResultBytes")

    model_config = {"populate_by_name": True}


class McpServerRegistry(BaseModel):
    servers: dict[str, McpServerConfig] = Field(default_factory=dict)

    def get(self, name: str) -> McpServerConfig | None:
        return self.servers.get(name)


@lru_cache(maxsize=1)
def load_mcp_registry() -> McpServerRegistry:
    raw = os.getenv("MCP_SERVER_REGISTRY", "").strip()
    if not raw:
        return McpServerRegistry()
    data = json.loads(raw)
    if "servers" in data:
        return McpServerRegistry.model_validate(data)
    return McpServerRegistry(servers=data)


def reset_mcp_registry_cache() -> None:
    load_mcp_registry.cache_clear()


def resolve_credential(credential_ref: str | None) -> str | None:
    """Resolve a credential reference. Only env:NAME is supported (no inline secrets)."""
    if not credential_ref:
        return None
    if credential_ref.startswith("env:"):
        name = credential_ref.removeprefix("env:").strip()
        if not name:
            raise ValueError("empty credential env name")
        value = os.getenv(name, "").strip()
        if not value:
            raise ValueError(f"credential env {name} is empty")
        return value
    if credential_ref.startswith("vault://"):
        raise ValueError("vault credential refs require a sidecar injector; use env:NAME")
    raise ValueError("unsupported credential ref; use env:NAME")


def _truncate_result(payload: Any, max_bytes: int) -> tuple[Any, bool]:
    encoded = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    if len(encoded) <= max_bytes:
        return payload, False
    return {
        "truncated": True,
        "message": "MCP tool result exceeded max_result_bytes",
        "max_result_bytes": max_bytes,
        "preview": encoded[: min(512, max_bytes)].decode("utf-8", errors="replace"),
    }, True


async def execute_mcp_tool(
    client: httpx.AsyncClient,
    *,
    server_name: str,
    tool_name: str,
    arguments: dict[str, Any],
    registry: McpServerRegistry | None = None,
) -> dict[str, Any]:
    """Execute a tool via MCP Streamable HTTP JSON-RPC tools/call."""
    registry = registry or load_mcp_registry()
    server = registry.get(server_name)
    if server is None:
        raise HTTPException(
            status_code=404,
            detail={
                "error": {
                    "type": "api_error",
                    "code": "mcp_server_not_found",
                    "message": f"MCP server '{server_name}' is not registered",
                }
            },
        )
    if server.transport not in {"http", "streamable-http"}:
        raise HTTPException(
            status_code=501,
            detail={
                "error": {
                    "type": "api_error",
                    "code": "mcp_transport_unsupported",
                    "message": f"MCP transport '{server.transport}' is not supported",
                }
            },
        )

    try:
        token = resolve_credential(server.credential_ref)
    except ValueError as error:
        raise HTTPException(
            status_code=503,
            detail={
                "error": {
                    "type": "api_error",
                    "code": "mcp_credential_unavailable",
                    "message": str(error),
                }
            },
        ) from error

    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"

    rpc_body = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": tool_name, "arguments": arguments},
    }
    try:
        response = await client.post(
            server.url.rstrip("/"),
            json=rpc_body,
            headers=headers,
            timeout=server.timeout_seconds,
        )
        response.raise_for_status()
    except httpx.HTTPError as error:
        raise HTTPException(
            status_code=502,
            detail={
                "error": {
                    "type": "api_error",
                    "code": "mcp_upstream_unavailable",
                    "message": "MCP server unavailable",
                },
                "reason": str(error),
            },
        ) from error

    try:
        payload = response.json()
    except ValueError as error:
        raise HTTPException(
            status_code=502,
            detail={
                "error": {
                    "type": "api_error",
                    "code": "mcp_invalid_response",
                    "message": "MCP server returned non-JSON body",
                }
            },
        ) from error

    result = payload.get("result", payload)
    filtered, truncated = _truncate_result(result, server.max_result_bytes)
    return {
        "server": server_name,
        "tool": tool_name,
        "transport": "streamable-http",
        "result": filtered,
        "truncated": truncated,
        "credential_ref": server.credential_ref,
    }
