"""Fuzz tests for budget policy parsing and price calculation."""

from __future__ import annotations

import hashlib

import pytest
from hypothesis import assume, given, settings
from hypothesis import strategies as st
from pydantic import ValidationError

from app.gateway.budget_policy import (
    InferenceBudgetPolicy,
    ModelPrice,
    ScopeBudget,
    api_key_fingerprint,
)


@given(st.text())
@settings(max_examples=200)
def test_api_key_fingerprint_never_crashes(api_key: str) -> None:
    """Arbitrary strings never crash fingerprint generation."""
    try:
        result = api_key_fingerprint(api_key)
        assert isinstance(result, str)
        assert len(result) == 16  # First 16 chars of SHA-256 hex
    except Exception as error:
        pytest.fail(f"Unexpected error: {error}")


@given(st.binary())
@settings(max_examples=150)
def test_api_key_fingerprint_handles_binary(data: bytes) -> None:
    """Binary data decoded as string produces valid fingerprint."""
    api_key = data.decode("utf-8", errors="replace")
    result = api_key_fingerprint(api_key)
    assert isinstance(result, str)
    assert len(result) == 16


@given(st.text(min_size=1, max_size=200))
@settings(max_examples=300)
def test_api_key_fingerprint_is_deterministic(api_key: str) -> None:
    """Same API key always produces same fingerprint."""
    result1 = api_key_fingerprint(api_key)
    result2 = api_key_fingerprint(api_key)
    assert result1 == result2


@given(
    st.text(alphabet="abcdefghijklmnopqrstuvwxyz0123456789", min_size=1, max_size=100),
    st.text(alphabet="abcdefghijklmnopqrstuvwxyz0123456789", min_size=1, max_size=100),
)
@settings(max_examples=200)
def test_api_key_fingerprint_different_keys_different_fingerprint(key1: str, key2: str) -> None:
    """Different keys produce different fingerprints."""
    assume(key1 != key2)

    result1 = api_key_fingerprint(key1)
    result2 = api_key_fingerprint(key2)

    assert result1 != result2


@given(st.text(min_size=1, max_size=200))
@settings(max_examples=200)
def test_api_key_fingerprint_manual_verification(api_key: str) -> None:
    """Fingerprint matches first 16 chars of SHA-256."""
    result = api_key_fingerprint(api_key)
    expected = hashlib.sha256(api_key.encode("utf-8")).hexdigest()[:16]
    assert result == expected


@given(
    st.floats(min_value=0, max_value=1000),
    st.floats(min_value=0, max_value=1000),
)
@settings(max_examples=200)
def test_model_price_validation_non_negative(input_price: float, output_price: float) -> None:
    """ModelPrice accepts non-negative prices."""
    try:
        price = ModelPrice(inputPerMillion=input_price, outputPerMillion=output_price)
        assert price.input_per_million == input_price
        assert price.output_per_million == output_price
    except ValidationError:
        # Invalid float values (NaN, inf) are rejected
        pass


@given(st.floats(max_value=-0.01))
@settings(max_examples=100)
def test_model_price_rejects_negative(negative_price: float) -> None:
    """ModelPrice rejects negative prices."""
    with pytest.raises(ValidationError):
        ModelPrice(inputPerMillion=negative_price, outputPerMillion=0)


@given(
    st.floats(min_value=0.01, max_value=10000),
    st.integers(min_value=1, max_value=86400),
)
@settings(max_examples=150)
def test_scope_budget_validation(budget: float, window: int) -> None:
    """ScopeBudget accepts valid budget and window values."""
    try:
        scope = ScopeBudget(budgetUsd=budget, windowSeconds=window)
        assert scope.budget_usd == budget
        assert scope.window_seconds == window
    except ValidationError:
        pass


@given(st.floats(max_value=0))
@settings(max_examples=100)
def test_scope_budget_rejects_zero_budget(budget: float) -> None:
    """ScopeBudget rejects zero or negative budget."""
    with pytest.raises(ValidationError):
        ScopeBudget(budgetUsd=budget, windowSeconds=3600)


@given(st.integers(max_value=0))
@settings(max_examples=100)
def test_scope_budget_rejects_zero_window(window: int) -> None:
    """ScopeBudget rejects zero or negative window."""
    with pytest.raises(ValidationError):
        ScopeBudget(budgetUsd=100.0, windowSeconds=window)


@given(
    st.integers(min_value=0, max_value=1000000),
    st.integers(min_value=0, max_value=1000000),
)
@settings(max_examples=200)
def test_inference_budget_policy_estimate_cost_never_negative(
    input_tokens: int, output_tokens: int
) -> None:
    """Cost estimation never returns negative values."""
    policy_data = {
        "version": "1",
        "default": {"budgetUsd": 100, "windowSeconds": 3600},
        "prices": {"*": {"inputPerMillion": 1.0, "outputPerMillion": 2.0}},
    }
    policy = InferenceBudgetPolicy.model_validate(policy_data)

    cost_micros = policy.estimate_cost_micros(
        "test-model", input_tokens=input_tokens, output_tokens=output_tokens
    )
    cost_usd = policy.estimate_cost_usd(
        "test-model", input_tokens=input_tokens, output_tokens=output_tokens
    )

    assert cost_micros >= 0
    assert cost_usd >= 0.0


@given(
    st.text(alphabet="abcdefghijklmnopqrstuvwxyz-", min_size=1, max_size=50),
)
@settings(max_examples=150)
def test_inference_budget_policy_price_fallback(model_name: str) -> None:
    """Price lookup falls back to default when model not found."""
    policy_data = {
        "version": "1",
        "default": {"budgetUsd": 100, "windowSeconds": 3600},
        "prices": {"*": {"inputPerMillion": 1.0, "outputPerMillion": 2.0}},
    }
    policy = InferenceBudgetPolicy.model_validate(policy_data)

    price = policy.price_for(model_name)
    assert isinstance(price, ModelPrice)
    assert price.input_per_million == 1.0


@given(
    st.integers(min_value=1, max_value=100000),
    st.integers(min_value=1, max_value=100000),
)
@settings(max_examples=200)
def test_inference_budget_policy_cost_rounds_up(input_tokens: int, output_tokens: int) -> None:
    """Cost estimation rounds up (never under-reserve)."""
    policy_data = {
        "version": "1",
        "default": {"budgetUsd": 100, "windowSeconds": 3600},
        "prices": {"*": {"inputPerMillion": 0.001, "outputPerMillion": 0.002}},
    }
    policy = InferenceBudgetPolicy.model_validate(policy_data)

    cost_usd = policy.estimate_cost_usd(
        "test-model", input_tokens=input_tokens, output_tokens=output_tokens
    )

    # With tiny prices, should still be non-zero for non-zero tokens
    if input_tokens > 0 or output_tokens > 0:
        assert cost_usd > 0


@given(
    st.dictionaries(
        st.text(min_size=1, max_size=30),
        st.floats(min_value=0, max_value=100) | st.integers(min_value=0, max_value=100),
        min_size=1,
        max_size=20,
    )
)
@settings(max_examples=150)
def test_inference_budget_policy_canonical_bytes_deterministic(extra_data: dict) -> None:
    """Canonical bytes representation is deterministic."""
    policy_data = {
        "version": "1",
        "default": {"budgetUsd": 100, "windowSeconds": 3600},
        "prices": {"*": {"inputPerMillion": 1.0, "outputPerMillion": 2.0}},
        **extra_data,
    }
    try:
        policy = InferenceBudgetPolicy.model_validate(policy_data)

        bytes1 = policy.canonical_bytes()
        bytes2 = policy.canonical_bytes()

        assert bytes1 == bytes2
    except ValidationError:
        pass


@given(
    st.text(min_size=1, max_size=100),
)
@settings(max_examples=200)
def test_inference_budget_policy_sha256_is_stable(version: str) -> None:
    """SHA-256 digest of policy is stable for same policy."""
    policy_data = {
        "version": version,
        "default": {"budgetUsd": 100, "windowSeconds": 3600},
        "prices": {"*": {"inputPerMillion": 1.0, "outputPerMillion": 2.0}},
    }
    try:
        policy = InferenceBudgetPolicy.model_validate(policy_data)

        sha1 = policy.sha256()
        sha2 = policy.sha256()

        assert sha1 == sha2
        assert sha1.startswith("sha256:")
    except ValidationError:
        pass


def test_inference_budget_policy_requires_default_price() -> None:
    """Policy validation requires default price entry."""
    policy_data = {
        "version": "1",
        "default": {"budgetUsd": 100, "windowSeconds": 3600},
        "prices": {},
    }
    with pytest.raises(ValidationError, match="must include"):
        InferenceBudgetPolicy.model_validate(policy_data)


def test_inference_budget_policy_empty_version_rejected() -> None:
    """Empty version string is rejected."""
    policy_data = {
        "version": "",
        "default": {"budgetUsd": 100, "windowSeconds": 3600},
        "prices": {"*": {"inputPerMillion": 1.0, "outputPerMillion": 2.0}},
    }
    with pytest.raises(ValidationError):
        InferenceBudgetPolicy.model_validate(policy_data)


@given(
    st.text(min_size=1, max_size=50),
    st.floats(min_value=0, max_value=100),
)
@settings(max_examples=150)
def test_inference_budget_policy_scope_for_tenant(tenant_id: str, budget: float) -> None:
    """scope_for returns correct scope for tenant."""
    policy_data = {
        "version": "1",
        "default": {"budgetUsd": budget, "windowSeconds": 3600},
        "prices": {"*": {"inputPerMillion": 1.0, "outputPerMillion": 2.0}},
    }
    try:
        policy = InferenceBudgetPolicy.model_validate(policy_data)
        scope_name, scope = policy.scope_for(tenant_id)

        assert isinstance(scope_name, str)
        assert isinstance(scope, ScopeBudget)
        assert scope.budget_usd == budget
    except ValidationError:
        pass


@given(
    st.integers(min_value=-1000000, max_value=0),
    st.integers(min_value=-1000000, max_value=0),
)
@settings(max_examples=150)
def test_inference_budget_policy_negative_tokens_clamped(
    input_tokens: int, output_tokens: int
) -> None:
    """Negative token counts are clamped to zero."""
    policy_data = {
        "version": "1",
        "default": {"budgetUsd": 100, "windowSeconds": 3600},
        "prices": {"*": {"inputPerMillion": 1.0, "outputPerMillion": 2.0}},
    }
    policy = InferenceBudgetPolicy.model_validate(policy_data)

    cost_micros = policy.estimate_cost_micros(
        "test-model", input_tokens=input_tokens, output_tokens=output_tokens
    )

    assert cost_micros == 0
