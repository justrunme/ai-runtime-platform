"""Assert the v2.0 closed-loop surface remains wired."""

from __future__ import annotations

from app.gateway.main import app


def test_closed_loop_routes_are_registered() -> None:
    paths = set(app.openapi()["paths"])
    required = {
        "/v1/chat/completions",
        "/v1/decisions/{request_id}",
        "/v1/runtime/status",
        "/v1/runtime/verify",
        "/v1/runtime/jwks",
        "/mcp/tools/{tool_name}/call",
        "/mcp/servers",
        "/livez",
        "/readyz",
        "/metrics",
    }
    missing = required - paths
    assert not missing, f"missing closed-loop routes: {sorted(missing)}"


def test_openapi_declares_runtime_verify() -> None:
    schema = app.openapi()
    assert "/v1/runtime/verify" in schema["paths"]
    assert "/v1/runtime/status" in schema["paths"]
    assert schema["info"]["version"].startswith("2.")
