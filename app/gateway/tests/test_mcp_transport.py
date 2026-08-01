"""MCP Streamable HTTP transport tests (JSON, SSE, sessions)."""

from __future__ import annotations

import json

import httpx
import pytest
from fastapi import HTTPException

from app.gateway.mcp_transport import (
    compute_capability_digest,
    execute_mcp_tool,
    initialize_mcp_session,
    load_mcp_registry,
    parse_mcp_response_body,
    reset_mcp_registry_cache,
    reset_mcp_sessions,
    resolve_credential,
)


@pytest.fixture(autouse=True)
def _reset_sessions() -> None:
    reset_mcp_sessions()
    yield
    reset_mcp_sessions()


@pytest.fixture
def registry(monkeypatch: pytest.MonkeyPatch) -> None:
    reset_mcp_registry_cache()
    monkeypatch.setenv(
        "MCP_SERVER_REGISTRY",
        json.dumps(
            {
                "docs": {
                    "url": "http://mcp-docs.test/mcp",
                    "transport": "http",
                    "credentialRef": "env:MCP_DOCS_TOKEN",
                    "maxResultBytes": 256,
                    "sessionMode": "never",
                }
            }
        ),
    )
    monkeypatch.setenv("MCP_DOCS_TOKEN", "secret-token")
    reset_mcp_registry_cache()
    yield
    reset_mcp_registry_cache()


@pytest.fixture
def session_registry(monkeypatch: pytest.MonkeyPatch) -> None:
    reset_mcp_registry_cache()
    monkeypatch.setenv(
        "MCP_SERVER_REGISTRY",
        json.dumps(
            {
                "docs": {
                    "url": "http://mcp-docs.test/mcp",
                    "transport": "streamable-http",
                    "credentialRef": "env:MCP_DOCS_TOKEN",
                    "sessionMode": "always",
                    "capabilityDigest": compute_capability_digest({"tools": {"listChanged": True}}),
                }
            }
        ),
    )
    monkeypatch.setenv("MCP_DOCS_TOKEN", "secret-token")
    reset_mcp_registry_cache()
    yield
    reset_mcp_registry_cache()


def test_resolve_credential_env_only(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TOOL_TOKEN", "abc")
    assert resolve_credential("env:TOOL_TOKEN") == "abc"
    with pytest.raises(ValueError):
        resolve_credential("inline:nope")


def test_parse_sse_jsonrpc() -> None:
    body = b'event: message\ndata: {"jsonrpc":"2.0","id":1,"result":{"ok":true}}\n\n'
    payload = parse_mcp_response_body("text/event-stream", body)
    assert payload["result"]["ok"] is True


@pytest.mark.anyio
async def test_execute_mcp_tool_streamable_http(registry) -> None:
    seen_auth: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_auth.append(request.headers.get("authorization", ""))
        body = json.loads(request.content)
        assert body["method"] == "tools/call"
        assert body["params"]["name"] == "search"
        return httpx.Response(
            200,
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "result": {"content": [{"type": "text", "text": "ok"}]},
            },
            request=request,
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await execute_mcp_tool(
            client,
            server_name="docs",
            tool_name="search",
            arguments={"q": "runtime"},
            registry=load_mcp_registry(),
        )
    assert result["truncated"] is False
    assert result["result"]["content"][0]["text"] == "ok"
    assert seen_auth == ["Bearer secret-token"]
    assert result["mcp_request_id"]


@pytest.mark.anyio
async def test_execute_mcp_tool_truncates_large_result(registry) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"jsonrpc": "2.0", "id": 1, "result": {"blob": "x" * 1000}},
            request=request,
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await execute_mcp_tool(
            client,
            server_name="docs",
            tool_name="search",
            arguments={},
            registry=load_mcp_registry(),
        )
    assert result["truncated"] is True
    assert result["result"]["truncated"] is True


@pytest.mark.anyio
async def test_execute_mcp_tool_parses_sse(registry) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=(
                b"event: message\n"
                b'data: {"jsonrpc":"2.0","id":"1","result":{"content":[{"type":"text","text":"sse"}]}}\n\n'
            ),
            headers={"content-type": "text/event-stream"},
            request=request,
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await execute_mcp_tool(
            client,
            server_name="docs",
            tool_name="search",
            arguments={},
            registry=load_mcp_registry(),
        )
    assert result["result"]["content"][0]["text"] == "sse"


@pytest.mark.anyio
async def test_initialize_and_session_tools_call(session_registry) -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        calls.append(body["method"])
        if body["method"] == "initialize":
            return httpx.Response(
                200,
                json={
                    "jsonrpc": "2.0",
                    "id": body["id"],
                    "result": {
                        "protocolVersion": "2024-11-05",
                        "capabilities": {"tools": {"listChanged": True}},
                        "serverInfo": {"name": "docs", "version": "1"},
                    },
                },
                headers={"Mcp-Session-Id": "sess-1"},
                request=request,
            )
        if body["method"] == "notifications/initialized":
            return httpx.Response(202, request=request)
        assert request.headers.get("mcp-session-id") == "sess-1"
        return httpx.Response(
            200,
            json={"jsonrpc": "2.0", "id": body["id"], "result": {"ok": True}},
            request=request,
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        session = await initialize_mcp_session(
            client, server_name="docs", registry=load_mcp_registry()
        )
        assert session.session_id == "sess-1"
        assert session.capability_digest.startswith("sha256:")
        result = await execute_mcp_tool(
            client,
            server_name="docs",
            tool_name="search",
            arguments={},
            registry=load_mcp_registry(),
        )
    assert result["session_id"] == "sess-1"
    assert "initialize" in calls
    assert "tools/call" in calls


@pytest.mark.anyio
async def test_capability_digest_mismatch(
    session_registry, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(
        "MCP_SERVER_REGISTRY",
        json.dumps(
            {
                "docs": {
                    "url": "http://mcp-docs.test/mcp",
                    "transport": "streamable-http",
                    "sessionMode": "always",
                    "capabilityDigest": "sha256:deadbeef",
                }
            }
        ),
    )
    reset_mcp_registry_cache()

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "jsonrpc": "2.0",
                "id": body["id"],
                "result": {"capabilities": {"tools": {}}, "protocolVersion": "2024-11-05"},
            },
            headers={"Mcp-Session-Id": "sess-x"},
            request=request,
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(HTTPException) as exc:
            await initialize_mcp_session(
                client, server_name="docs", registry=load_mcp_registry(), force=True
            )
    assert exc.value.status_code == 409
    assert exc.value.detail["error"]["code"] == "mcp_capability_digest_mismatch"
