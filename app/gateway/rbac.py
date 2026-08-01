"""Coarse runtime RBAC from verified JWT claims."""

from __future__ import annotations

import os
from collections.abc import Iterable
from typing import Any

from fastapi import HTTPException, Request

from app.gateway.jwt_verify import is_jwt_verify_enabled

PLATFORM_ADMIN_ROLES = frozenset({"platform-admin", "ai-platform-admins"})
RUNTIME_SERVICE_ROLES = frozenset({"runtime-service", "ai-runtime-service"})
AUDITOR_ROLES = frozenset({"ai-auditors", "global-auditor", "auditor"})


def _normalize(values: Any) -> set[str]:
    if values is None:
        return set()
    if isinstance(values, str):
        return {part.strip() for part in values.split(",") if part.strip()}
    if isinstance(values, (list, tuple, set)):
        return {str(item).strip() for item in values if str(item).strip()}
    return set()


def identity_roles(request: Request) -> set[str]:
    claims = getattr(request.state, "identity_claims", None)
    if not isinstance(claims, dict):
        return set()
    roles: set[str] = set()
    roles |= _normalize(claims.get("groups"))
    roles |= _normalize(claims.get("roles"))
    realm = claims.get("realm_access")
    if isinstance(realm, dict):
        roles |= _normalize(realm.get("roles"))
    return roles


def rbac_enforced() -> bool:
    """Enforce role checks when JWT verify is on, or when explicitly forced."""
    if os.getenv("GATEWAY_ENFORCE_RUNTIME_RBAC", "").strip().lower() in {"1", "true", "yes"}:
        return True
    return is_jwt_verify_enabled()


def require_any_role(request: Request, allowed: Iterable[str]) -> None:
    if not rbac_enforced():
        return
    allowed_set = {str(item) for item in allowed}
    if identity_roles(request).intersection(allowed_set):
        return
    raise HTTPException(
        status_code=403,
        detail={
            "error": {
                "type": "permission_error",
                "code": "runtime_role_required",
                "message": "caller lacks a required runtime role",
            }
        },
    )


STATUS_ROLES = PLATFORM_ADMIN_ROLES | RUNTIME_SERVICE_ROLES | AUDITOR_ROLES
VERIFY_ROLES = PLATFORM_ADMIN_ROLES | RUNTIME_SERVICE_ROLES
MCP_SERVERS_ROLES = PLATFORM_ADMIN_ROLES | AUDITOR_ROLES
