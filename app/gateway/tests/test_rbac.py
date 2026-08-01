"""Runtime RBAC for status/verify/MCP registry."""

from __future__ import annotations

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from app.gateway.rbac import STATUS_ROLES, require_any_role


def _request(claims: dict | None) -> Request:
    scope = {
        "type": "http",
        "headers": [],
        "method": "GET",
        "path": "/v1/runtime/status",
        "query_string": b"",
        "client": ("testclient", 50000),
        "server": ("testserver", 80),
        "scheme": "http",
        "root_path": "",
    }
    request = Request(scope)
    request.state.identity_claims = claims or {}
    return request


def test_rbac_allows_matching_group(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GATEWAY_ENFORCE_RUNTIME_RBAC", "true")
    require_any_role(
        _request({"groups": ["platform-admin"]}),
        STATUS_ROLES,
    )


def test_rbac_denies_tenant_user(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GATEWAY_ENFORCE_RUNTIME_RBAC", "true")
    with pytest.raises(HTTPException) as error:
        require_any_role(_request({"groups": ["finance"], "tenant": "finance"}), STATUS_ROLES)
    assert error.value.status_code == 403
    assert error.value.detail["error"]["code"] == "runtime_role_required"
