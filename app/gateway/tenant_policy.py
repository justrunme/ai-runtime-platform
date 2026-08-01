"""Per-tenant runtime policy: allowlists and admission limits."""

from __future__ import annotations

import json
import os
from functools import lru_cache

from pydantic import BaseModel, Field


class TenantRuntimePolicy(BaseModel):
    allowed_models: list[str] = Field(default_factory=list, alias="allowedModels")
    allowed_routes: list[str] = Field(default_factory=list, alias="allowedRoutes")
    max_concurrent_requests: int = Field(default=50, ge=1, alias="maxConcurrentRequests")
    max_queued_requests: int = Field(default=100, ge=0, alias="maxQueuedRequests")
    upstream_credential_ref: str | None = Field(default=None, alias="upstreamCredentialRef")

    model_config = {"populate_by_name": True}


class TenantPolicyBundle(BaseModel):
    tenants: dict[str, TenantRuntimePolicy] = Field(default_factory=dict)
    default: TenantRuntimePolicy = Field(default_factory=TenantRuntimePolicy)
    auditor_groups: list[str] = Field(
        default_factory=lambda: ["ai-auditors", "global-auditor"],
        alias="auditorGroups",
    )

    model_config = {"populate_by_name": True}

    def for_tenant(self, tenant_id: str) -> TenantRuntimePolicy:
        return self.tenants.get(tenant_id, self.default)

    def model_allowed(self, tenant_id: str, model: str | None) -> bool:
        policy = self.for_tenant(tenant_id)
        if not policy.allowed_models:
            return True
        return str(model or "") in policy.allowed_models

    def route_allowed(self, tenant_id: str, route: str | None) -> bool:
        policy = self.for_tenant(tenant_id)
        if not policy.allowed_routes:
            return True
        if route is None:
            return True
        return route in policy.allowed_routes


@lru_cache(maxsize=1)
def load_tenant_policy_bundle() -> TenantPolicyBundle:
    raw = os.getenv("TENANT_RUNTIME_POLICY", "").strip()
    if not raw:
        return TenantPolicyBundle()
    data = json.loads(raw)
    if "tenants" not in data and isinstance(data, dict):
        # Accept the compact {finance: {...}, research: {...}} shape from the roadmap.
        return TenantPolicyBundle(tenants=data)
    return TenantPolicyBundle.model_validate(data)


def reset_tenant_policy_cache() -> None:
    load_tenant_policy_bundle.cache_clear()
