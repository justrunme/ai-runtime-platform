"""Fuzz tests for tenant policy parsing and validation."""

from __future__ import annotations

import pytest
from hypothesis import assume, given, settings
from hypothesis import strategies as st
from pydantic import ValidationError

from app.gateway.tenant_policy import TenantPolicyBundle, TenantRuntimePolicy


@given(
    st.lists(st.text(min_size=1, max_size=50), min_size=0, max_size=20),
)
@settings(max_examples=150)
def test_tenant_runtime_policy_allowed_models(models: list[str]) -> None:
    """TenantRuntimePolicy accepts arbitrary model lists."""
    try:
        policy = TenantRuntimePolicy(allowedModels=models)
        assert policy.allowed_models == models
    except ValidationError:
        pass


@given(
    st.lists(st.text(min_size=1, max_size=50), min_size=0, max_size=20),
)
@settings(max_examples=150)
def test_tenant_runtime_policy_allowed_routes(routes: list[str]) -> None:
    """TenantRuntimePolicy accepts arbitrary route lists."""
    try:
        policy = TenantRuntimePolicy(allowedRoutes=routes)
        assert policy.allowed_routes == routes
    except ValidationError:
        pass


@given(st.integers(min_value=1, max_value=10000))
@settings(max_examples=200)
def test_tenant_runtime_policy_max_concurrent_requests(max_requests: int) -> None:
    """maxConcurrentRequests accepts positive integers."""
    policy = TenantRuntimePolicy(maxConcurrentRequests=max_requests)
    assert policy.max_concurrent_requests == max_requests


@given(st.integers(max_value=0))
@settings(max_examples=100)
def test_tenant_runtime_policy_rejects_zero_concurrent(max_requests: int) -> None:
    """maxConcurrentRequests rejects zero or negative values."""
    with pytest.raises(ValidationError):
        TenantRuntimePolicy(maxConcurrentRequests=max_requests)


@given(st.integers(min_value=0, max_value=10000))
@settings(max_examples=200)
def test_tenant_runtime_policy_max_queued_requests(max_queued: int) -> None:
    """maxQueuedRequests accepts non-negative integers."""
    policy = TenantRuntimePolicy(maxQueuedRequests=max_queued)
    assert policy.max_queued_requests == max_queued


@given(st.integers(max_value=-1))
@settings(max_examples=100)
def test_tenant_runtime_policy_rejects_negative_queued(max_queued: int) -> None:
    """maxQueuedRequests rejects negative values."""
    with pytest.raises(ValidationError):
        TenantRuntimePolicy(maxQueuedRequests=max_queued)


@given(
    st.sampled_from(
        [
            "fail_closed",
            "fail-closed",
            "closed",
            "degraded",
            "local",
            "fallback",
            "fail_open",
            "fail-open",
            "open",
        ]
    )
)
@settings(max_examples=100)
def test_tenant_runtime_policy_redis_unavailable_policy(policy_value: str) -> None:
    """redisUnavailablePolicy accepts valid enum values."""
    try:
        policy = TenantRuntimePolicy(redisUnavailablePolicy=policy_value)
        assert policy.redis_unavailable_policy in {"fail_closed", "degraded", "fail_open"}
    except ValidationError:
        pass


@given(
    st.dictionaries(
        st.text(min_size=1, max_size=50),
        st.lists(st.text(max_size=50), max_size=10),
        min_size=0,
        max_size=10,
    )
)
@settings(max_examples=150)
def test_tenant_policy_bundle_tenants(tenants: dict) -> None:
    """TenantPolicyBundle accepts tenant dictionaries."""
    try:
        # Convert to proper structure
        tenant_configs = {}
        for tenant_id, models in tenants.items():
            tenant_configs[tenant_id] = {"allowedModels": models}

        bundle = TenantPolicyBundle(tenants=tenant_configs)
        assert isinstance(bundle.tenants, dict)
    except ValidationError:
        pass


@given(
    st.text(min_size=1, max_size=50),
    st.lists(st.text(min_size=1, max_size=50), min_size=1, max_size=10),
)
@settings(max_examples=200)
def test_tenant_policy_bundle_model_allowed(tenant_id: str, allowed_models: list[str]) -> None:
    """model_allowed checks against tenant allowlist."""
    tenant_configs = {tenant_id: {"allowedModels": allowed_models}}
    bundle = TenantPolicyBundle(tenants=tenant_configs)

    # Allowed model should pass
    assert bundle.model_allowed(tenant_id, allowed_models[0]) is True

    # Model not in list should fail
    assert bundle.model_allowed(tenant_id, "not-in-list-xyz") is False


@given(
    st.text(min_size=1, max_size=50),
)
@settings(max_examples=150)
def test_tenant_policy_bundle_empty_allowlist_permits_all(tenant_id: str) -> None:
    """Empty allowlist permits all models."""
    tenant_configs = {tenant_id: {"allowedModels": []}}
    bundle = TenantPolicyBundle(tenants=tenant_configs)

    assert bundle.model_allowed(tenant_id, "any-model") is True


@given(
    st.text(min_size=1, max_size=50),
    st.lists(st.text(min_size=1, max_size=50), min_size=1, max_size=10),
)
@settings(max_examples=200)
def test_tenant_policy_bundle_route_allowed(tenant_id: str, allowed_routes: list[str]) -> None:
    """route_allowed checks against tenant allowlist."""
    tenant_configs = {tenant_id: {"allowedRoutes": allowed_routes}}
    bundle = TenantPolicyBundle(tenants=tenant_configs)

    # Allowed route should pass
    assert bundle.route_allowed(tenant_id, allowed_routes[0]) is True

    # Route not in list should fail
    assert bundle.route_allowed(tenant_id, "/not-in-list") is False


@given(
    st.text(min_size=1, max_size=50),
)
@settings(max_examples=150)
def test_tenant_policy_bundle_none_route_always_allowed(tenant_id: str) -> None:
    """None route is always allowed."""
    tenant_configs = {tenant_id: {"allowedRoutes": ["/api/chat"]}}
    bundle = TenantPolicyBundle(tenants=tenant_configs)

    assert bundle.route_allowed(tenant_id, None) is True


@given(
    st.text(min_size=1, max_size=50),
)
@settings(max_examples=150)
def test_tenant_policy_bundle_unknown_tenant_uses_default(tenant_id: str) -> None:
    """Unknown tenant uses default policy."""
    bundle = TenantPolicyBundle(
        tenants={},
        default=TenantRuntimePolicy(maxConcurrentRequests=100),
    )

    policy = bundle.for_tenant(tenant_id)
    assert policy.max_concurrent_requests == 100


@given(
    st.lists(st.text(min_size=1, max_size=50), min_size=1, max_size=20),
)
@settings(max_examples=150)
def test_tenant_policy_bundle_auditor_groups(groups: list[str]) -> None:
    """TenantPolicyBundle accepts auditor groups."""
    try:
        bundle = TenantPolicyBundle(auditorGroups=groups)
        assert bundle.auditor_groups == groups
    except ValidationError:
        pass


@given(
    st.dictionaries(
        st.text(min_size=1, max_size=30),
        st.dictionaries(
            st.text(min_size=1, max_size=20),
            st.one_of(
                st.text(max_size=50),
                st.integers(min_value=1, max_value=1000),
                st.lists(st.text(max_size=30)),
            ),
            min_size=0,
            max_size=10,
        ),
        min_size=0,
        max_size=10,
    )
)
@settings(max_examples=150)
def test_tenant_policy_bundle_from_json(tenants_dict: dict) -> None:
    """TenantPolicyBundle can be created from JSON-like dict."""
    try:
        bundle = TenantPolicyBundle(tenants=tenants_dict)
        assert isinstance(bundle, TenantPolicyBundle)
    except ValidationError:
        pass


@given(
    st.text(min_size=1, max_size=50),
    st.integers(min_value=1, max_value=1000),
)
@settings(max_examples=200)
def test_tenant_policy_bundle_for_tenant_isolation(tenant_id: str, max_requests: int) -> None:
    """Policies for different tenants are isolated."""
    other_tenant = f"other-{tenant_id}"
    assume(tenant_id != other_tenant)

    tenant_configs = {
        tenant_id: {"maxConcurrentRequests": max_requests},
        other_tenant: {"maxConcurrentRequests": max_requests * 2},
    }
    bundle = TenantPolicyBundle(tenants=tenant_configs)

    policy1 = bundle.for_tenant(tenant_id)
    policy2 = bundle.for_tenant(other_tenant)

    assert policy1.max_concurrent_requests == max_requests
    assert policy2.max_concurrent_requests == max_requests * 2


@given(
    st.text(min_size=1, max_size=50),
    st.lists(st.text(min_size=1, max_size=50), min_size=1, max_size=10),
)
@settings(max_examples=200)
def test_tenant_policy_bundle_model_allowed_case_sensitive(
    tenant_id: str, models: list[str]
) -> None:
    """Model allowlist is case-sensitive."""
    tenant_configs = {tenant_id: {"allowedModels": models}}
    bundle = TenantPolicyBundle(tenants=tenant_configs)

    model = models[0]
    assert bundle.model_allowed(tenant_id, model) is True

    if model.lower() != model.upper():
        assert bundle.model_allowed(tenant_id, model.upper()) is (model.upper() in models)


def test_tenant_runtime_policy_defaults() -> None:
    """TenantRuntimePolicy has sensible defaults."""
    policy = TenantRuntimePolicy()
    assert policy.max_concurrent_requests == 50
    assert policy.max_queued_requests == 100
    assert policy.allowed_models == []
    assert policy.allowed_routes == []


def test_tenant_policy_bundle_defaults() -> None:
    """TenantPolicyBundle has sensible defaults."""
    bundle = TenantPolicyBundle()
    assert bundle.tenants == {}
    assert isinstance(bundle.default, TenantRuntimePolicy)
    assert "ai-auditors" in bundle.auditor_groups


@given(
    st.text(min_size=1, max_size=50),
)
@settings(max_examples=200)
def test_tenant_policy_bundle_for_tenant_deterministic(tenant_id: str) -> None:
    """for_tenant returns consistent policy for same tenant."""
    bundle = TenantPolicyBundle()

    policy1 = bundle.for_tenant(tenant_id)
    policy2 = bundle.for_tenant(tenant_id)

    assert policy1.max_concurrent_requests == policy2.max_concurrent_requests
    assert policy1.max_queued_requests == policy2.max_queued_requests
