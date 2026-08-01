"""Tenant identity extraction — JWT claims only when verification is enabled."""

from __future__ import annotations

from typing import Any

from fastapi import Request

from app.gateway.jwt_verify import is_jwt_verify_enabled
from app.gateway.tenant_policy import TenantPolicyBundle, load_tenant_policy_bundle


def _claims(request: Request) -> dict[str, Any]:
    state = getattr(request, "state", None)
    claims = getattr(state, "identity_claims", None) if state is not None else None
    return claims if isinstance(claims, dict) else {}


def _normalize_groups(claims: dict[str, Any]) -> list[str]:
    value = claims.get("groups") or claims.get("roles") or []
    if isinstance(value, str):
        return [part.strip() for part in value.split(",") if part.strip()]
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    return []


def resolve_tenant_id(request: Request) -> str:
    """Resolve tenant exclusively from verified JWT claims when JWT auth is on.

    Without JWT verification (local/demo), fall back to a stable default so
    decision records remain addressable. Client spoof headers are ignored.
    """
    claims = _claims(request)
    for key in ("tenant_id", "tenant", "team"):
        value = claims.get(key)
        if value not in (None, ""):
            return str(value).strip()
    if is_jwt_verify_enabled():
        # Authenticated but untentanted token — isolate into a dedicated bucket.
        subject = str(claims.get("sub") or claims.get("preferred_username") or "unknown")
        return f"user:{subject}"
    return "platform"


def is_global_auditor(
    request: Request,
    bundle: TenantPolicyBundle | None = None,
) -> bool:
    policy = bundle or load_tenant_policy_bundle()
    groups = set(_normalize_groups(_claims(request)))
    return bool(groups.intersection(policy.auditor_groups))
