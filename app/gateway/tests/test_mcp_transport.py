"""MCP Streamable HTTP transport tests."""

from __future__ import annotations

import json

import httpx
import pytest

from app.gateway.mcp_transport import (
    execute_mcp_tool,
    load_mcp_registry,
    reset_mcp_registry_cache,
    resolve_credential,
)


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
