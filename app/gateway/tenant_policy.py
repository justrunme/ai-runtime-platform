"""Per-tenant runtime policy: allowlists and admission limits."""

from __future__ import annotations

import json
import os
from functools import lru_cache
from typing import Literal

from pydantic import AliasChoices, BaseModel, Field

RedisUnavailablePolicy = Literal["fail_closed", "degraded", "fail_open"]


def _default_redis_unavailable_policy() -> RedisUnavailablePolicy:
    raw = os.getenv("ADMISSION_REDIS_UNAVAILABLE_POLICY", "fail_closed").strip().lower()
    if raw in {"fail_closed", "fail-closed", "closed"}:
        return "fail_closed"
    if raw in {"degraded", "local", "fallback"}:
        return "degraded"
    if raw in {"fail_open", "fail-open", "open"}:
        return "fail_open"
    return "fail_closed"


class TenantRuntimePolicy(BaseModel):
    allowed_models: list[str] = Field(default_factory=list, alias="allowedModels")
    allowed_routes: list[str] = Field(default_factory=list, alias="allowedRoutes")
    # Fleet-wide when Redis is configured; process-local otherwise.
    # Legacy PerReplica aliases remain accepted for older manifests.
    max_concurrent_requests: int = Field(
        default=50,
        ge=1,
        validation_alias=AliasChoices(
            "maxConcurrentRequests",
            "maxConcurrentRequestsPerReplica",
        ),
        serialization_alias="maxConcurrentRequests",
    )
    max_queued_requests: int = Field(
        default=100,
        ge=0,
        validation_alias=AliasChoices(
            "maxQueuedRequests",
            "maxQueuedRequestsPerReplica",
        ),
        serialization_alias="maxQueuedRequests",
    )
    redis_unavailable_policy: RedisUnavailablePolicy = Field(
        default_factory=_default_redis_unavailable_policy,
        validation_alias=AliasChoices(
            "redisUnavailablePolicy",
            "admissionRedisUnavailablePolicy",
        ),
        serialization_alias="redisUnavailablePolicy",
    )
    # Reserved: not applied to upstream Authorization yet (v2.1+).
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
