"""MCP execution transport (Streamable HTTP) with sessions, SSE, and credentials."""

from __future__ import annotations

import hashlib
import json
import os
import time
import uuid
from contextlib import suppress
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any

import httpx
from fastapi import HTTPException
from pydantic import BaseModel, Field

from app.gateway.evidence import runtime_version


class McpServerConfig(BaseModel):
    url: str
    transport: str = "http"  # http | streamable-http | sse
    credential_ref: str | None = Field(default=None, alias="credentialRef")
    timeout_seconds: float = Field(default=30.0, gt=0, alias="timeoutSeconds")
    max_result_bytes: int = Field(default=1_048_576, ge=64, alias="maxResultBytes")
    session_mode: str = Field(default="auto", alias="sessionMode")  # auto | always | never
    capability_digest: str | None = Field(default=None, alias="capabilityDigest")

    model_config = {"populate_by_name": True}


class McpServerRegistry(BaseModel):
    servers: dict[str, McpServerConfig] = Field(default_factory=dict)

    def get(self, name: str) -> McpServerConfig | None:
        return self.servers.get(name)


@dataclass
class McpSession:
    server_name: str
    session_id: str
    protocol_version: str | None = None
    capabilities: dict[str, Any] = field(default_factory=dict)
    capability_digest: str | None = None
    created_at: float = field(default_factory=time.time)


@dataclass
class McpSessionStore:
    """Process-local MCP sessions keyed by server name."""

    _sessions: dict[str, McpSession] = field(default_factory=dict)

    def get(self, server_name: str) -> McpSession | None:
        return self._sessions.get(server_name)

    def put(self, session: McpSession) -> None:
        self._sessions[session.server_name] = session

    def drop(self, server_name: str) -> None:
        self._sessions.pop(server_name, None)


_SESSION_STORE = McpSessionStore()


def session_store() -> McpSessionStore:
    return _SESSION_STORE


def reset_mcp_sessions() -> None:
    _SESSION_STORE._sessions.clear()


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


def compute_capability_digest(capabilities: dict[str, Any]) -> str:
    payload = json.dumps(capabilities, sort_keys=True, separators=(",", ":"))
    return f"sha256:{hashlib.sha256(payload.encode('utf-8')).hexdigest()}"


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


def parse_mcp_response_body(content_type: str, body: bytes) -> dict[str, Any]:
    """Parse Streamable HTTP JSON or SSE (`text/event-stream`) MCP responses."""
    ctype = (content_type or "").lower()
    text = body.decode("utf-8", errors="replace").strip()
    if "text/event-stream" in ctype or text.startswith("event:") or text.startswith("data:"):
        return _parse_sse_jsonrpc(text)
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as error:
        raise ValueError("MCP server returned non-JSON body") from error
    if not isinstance(payload, dict):
        raise ValueError("MCP server returned non-object JSON")
    return payload


def _parse_sse_jsonrpc(text: str) -> dict[str, Any]:
    """Extract the last JSON-RPC message from an SSE stream."""
    last: dict[str, Any] | None = None
    data_lines: list[str] = []
    for raw_line in text.splitlines():
        line = raw_line.rstrip("\r")
        if line.startswith("data:"):
            data_lines.append(line[5:].lstrip())
            continue
        if line == "" and data_lines:
            blob = "\n".join(data_lines).strip()
            data_lines = []
            if not blob or blob == "[DONE]":
                continue
            try:
                parsed = json.loads(blob)
            except json.JSONDecodeError:
                continue
            if isinstance(parsed, dict):
                last = parsed
    if data_lines:
        blob = "\n".join(data_lines).strip()
        if blob and blob != "[DONE]":
            try:
                parsed = json.loads(blob)
            except json.JSONDecodeError:
                parsed = None
            if isinstance(parsed, dict):
                last = parsed
    if last is None:
        raise ValueError("MCP SSE stream contained no JSON-RPC message")
    return last


def _auth_headers(token: str | None) -> dict[str, str]:
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


async def _post_rpc(
    client: httpx.AsyncClient,
    *,
    url: str,
    headers: dict[str, str],
    body: dict[str, Any],
    timeout: float,
) -> tuple[httpx.Response, dict[str, Any]]:
    response = await client.post(url, json=body, headers=headers, timeout=timeout)
    response.raise_for_status()
    payload = parse_mcp_response_body(response.headers.get("content-type", ""), response.content)
    return response, payload


async def initialize_mcp_session(
    client: httpx.AsyncClient,
    *,
    server_name: str,
    registry: McpServerRegistry | None = None,
    force: bool = False,
) -> McpSession:
    """Run MCP initialize and cache Mcp-Session-Id for subsequent tools/call."""
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
    existing = session_store().get(server_name)
    if existing is not None and not force:
        return existing

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

    headers = _auth_headers(token)
    init_body = {
        "jsonrpc": "2.0",
        "id": str(uuid.uuid4()),
        "method": "initialize",
        "params": {
            "protocolVersion": "2024-11-05",
            "capabilities": {"tools": {}},
            "clientInfo": {"name": "ai-runtime-platform", "version": runtime_version()},
        },
    }
    try:
        response, payload = await _post_rpc(
            client,
            url=server.url.rstrip("/"),
            headers=headers,
            body=init_body,
            timeout=server.timeout_seconds,
        )
    except httpx.HTTPError as error:
        raise HTTPException(
            status_code=502,
            detail={
                "error": {
                    "type": "api_error",
                    "code": "mcp_upstream_unavailable",
                    "message": "MCP initialize failed",
                },
                "reason": str(error),
            },
        ) from error
    except ValueError as error:
        raise HTTPException(
            status_code=502,
            detail={
                "error": {
                    "type": "api_error",
                    "code": "mcp_invalid_response",
                    "message": str(error),
                }
            },
        ) from error

    if "error" in payload:
        raise HTTPException(
            status_code=502,
            detail={
                "error": {
                    "type": "api_error",
                    "code": "mcp_initialize_error",
                    "message": str(payload["error"]),
                }
            },
        )

    result = payload.get("result") if isinstance(payload.get("result"), dict) else {}
    capabilities = (
        result.get("capabilities") if isinstance(result.get("capabilities"), dict) else {}
    )
    digest = compute_capability_digest(capabilities)
    if server.capability_digest and server.capability_digest != digest:
        raise HTTPException(
            status_code=409,
            detail={
                "error": {
                    "type": "api_error",
                    "code": "mcp_capability_digest_mismatch",
                    "message": "MCP server capabilities do not match configured digest",
                    "expected": server.capability_digest,
                    "actual": digest,
                }
            },
        )

    session_id = (
        response.headers.get("mcp-session-id")
        or response.headers.get("Mcp-Session-Id")
        or str(result.get("sessionId") or uuid.uuid4())
    )
    session = McpSession(
        server_name=server_name,
        session_id=str(session_id),
        protocol_version=str(result.get("protocolVersion") or "2024-11-05"),
        capabilities=capabilities,
        capability_digest=digest,
    )
    session_store().put(session)

    # Best-effort notifications/initialized (streamable HTTP).
    notify_headers = dict(headers)
    notify_headers["Mcp-Session-Id"] = session.session_id
    with suppress(httpx.HTTPError):
        await client.post(
            server.url.rstrip("/"),
            json={"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}},
            headers=notify_headers,
            timeout=min(5.0, server.timeout_seconds),
        )
    return session


async def cancel_mcp_request(
    client: httpx.AsyncClient,
    *,
    server_name: str,
    request_id: str,
    registry: McpServerRegistry | None = None,
) -> dict[str, Any]:
    """Send tools/call cancellation for an in-flight MCP request id."""
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

    headers = _auth_headers(token)
    session = session_store().get(server_name)
    if session is not None:
        headers["Mcp-Session-Id"] = session.session_id
    body = {
        "jsonrpc": "2.0",
        "method": "notifications/cancelled",
        "params": {"requestId": request_id, "reason": "client cancelled"},
    }
    try:
        response = await client.post(
            server.url.rstrip("/"),
            json=body,
            headers=headers,
            timeout=min(5.0, server.timeout_seconds),
        )
        # Some servers accept DELETE on the session to cancel.
        if response.status_code >= 400 and session is not None:
            await client.request(
                "DELETE",
                server.url.rstrip("/"),
                headers=headers,
                timeout=min(5.0, server.timeout_seconds),
            )
    except httpx.HTTPError as error:
        raise HTTPException(
            status_code=502,
            detail={
                "error": {
                    "type": "api_error",
                    "code": "mcp_cancel_failed",
                    "message": "MCP cancellation failed",
                },
                "reason": str(error),
            },
        ) from error
    return {"server": server_name, "request_id": request_id, "cancelled": True}


def _wants_session(server: McpServerConfig) -> bool:
    mode = (server.session_mode or "auto").lower()
    if mode == "always":
        return True
    if mode == "never":
        return False
    # auto: session for streamable-http / sse transports
    return server.transport in {"streamable-http", "sse"}


async def execute_mcp_tool(
    client: httpx.AsyncClient,
    *,
    server_name: str,
    tool_name: str,
    arguments: dict[str, Any],
    registry: McpServerRegistry | None = None,
    progress_token: str | None = None,
) -> dict[str, Any]:
    """Execute a tool via MCP Streamable HTTP (JSON or SSE) tools/call."""
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
    if server.transport not in {"http", "streamable-http", "sse"}:
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

    session: McpSession | None = None
    if _wants_session(server):
        session = await initialize_mcp_session(client, server_name=server_name, registry=registry)

    headers = _auth_headers(token)
    if session is not None:
        headers["Mcp-Session-Id"] = session.session_id

    rpc_id = str(uuid.uuid4())
    params: dict[str, Any] = {"name": tool_name, "arguments": arguments}
    if progress_token:
        params["_meta"] = {"progressToken": progress_token}
    rpc_body = {
        "jsonrpc": "2.0",
        "id": rpc_id,
        "method": "tools/call",
        "params": params,
    }
    try:
        response, payload = await _post_rpc(
            client,
            url=server.url.rstrip("/"),
            headers=headers,
            body=rpc_body,
            timeout=server.timeout_seconds,
        )
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
                "mcp_request_id": rpc_id,
            },
        ) from error
    except ValueError as error:
        raise HTTPException(
            status_code=502,
            detail={
                "error": {
                    "type": "api_error",
                    "code": "mcp_invalid_response",
                    "message": str(error),
                },
                "mcp_request_id": rpc_id,
            },
        ) from error

    # Refresh session id if upstream rotates it.
    rotated = response.headers.get("mcp-session-id") or response.headers.get("Mcp-Session-Id")
    if session is not None and rotated:
        session.session_id = str(rotated)
        session_store().put(session)

    if "error" in payload:
        raise HTTPException(
            status_code=502,
            detail={
                "error": {
                    "type": "api_error",
                    "code": "mcp_tool_error",
                    "message": str(payload["error"]),
                },
                "mcp_request_id": rpc_id,
            },
        )

    result = payload.get("result", payload)
    filtered, truncated = _truncate_result(result, server.max_result_bytes)
    return {
        "server": server_name,
        "tool": tool_name,
        "transport": server.transport,
        "result": filtered,
        "truncated": truncated,
        "credential_ref": server.credential_ref,
        "mcp_request_id": rpc_id,
        "session_id": session.session_id if session else None,
        "capability_digest": session.capability_digest if session else None,
        "response_content_type": response.headers.get("content-type"),
    }
