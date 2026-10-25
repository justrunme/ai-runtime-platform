"""Fuzz tests for request digest computation."""

from __future__ import annotations

import hashlib
import json

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from app.gateway.request_digest import binding_payload, compute_request_digest


@given(
    st.dictionaries(
        st.text(min_size=1, max_size=50),
        st.one_of(
            st.text(max_size=100),
            st.integers(),
            st.booleans(),
            st.none(),
            st.lists(st.text(max_size=50)),
        ),
        min_size=0,
        max_size=100,
    )
)
@settings(max_examples=250)
def test_compute_request_digest_never_crashes(request: dict) -> None:
    """Arbitrary request dicts never crash digest computation."""
    try:
        result = compute_request_digest(request)
        assert isinstance(result, str)
        assert len(result) == 64  # SHA-256 hex digest
    except Exception as error:
        pytest.fail(f"Unexpected error: {error}")


@given(
    st.dictionaries(
        st.text(alphabet="abcdefghijklmnopqrstuvwxyz_", min_size=1, max_size=20),
        st.one_of(st.text(max_size=50), st.integers(-1000, 1000), st.booleans()),
        min_size=0,
        max_size=50,
    )
)
@settings(max_examples=300)
def test_compute_request_digest_is_deterministic(request: dict) -> None:
    """Same request always produces same digest."""
    digest1 = compute_request_digest(request)
    digest2 = compute_request_digest(request)
    assert digest1 == digest2


@given(
    st.dictionaries(
        st.text(min_size=1, max_size=30),
        st.one_of(st.text(max_size=50), st.integers()),
        min_size=1,
        max_size=30,
    )
)
@settings(max_examples=200)
def test_compute_request_digest_changes_on_field_modification(request: dict) -> None:
    """Digest changes when any binding field changes."""
    digest1 = compute_request_digest(request)

    # Add or modify a binding field
    modified = dict(request)
    modified["subject"] = "modified_value"
    digest2 = compute_request_digest(modified)

    if "subject" not in request or request.get("subject") != "modified_value":
        assert digest1 != digest2


@given(
    st.dictionaries(
        st.text(min_size=1, max_size=20),
        st.text(max_size=50),
        min_size=1,
        max_size=20,
    )
)
@settings(max_examples=150)
def test_binding_payload_only_includes_known_fields(request: dict) -> None:
    """Only known binding fields are included in the payload."""
    payload = binding_payload(request)
    assert isinstance(payload, dict)

    # All keys in payload should be from the known binding fields
    known_fields = {
        "subject",
        "groups",
        "policy_pack",
        "team",
        "tenant_id",
        "owner",
        "environment",
        "namespace",
        "action",
        "model",
        "provider",
        "input_tokens",
        "output_tokens",
        "cost_per_request_usd",
        "cost_per_hour_usd",
        "month_to_date_cost_usd",
        "forecast_monthly_cost_usd",
        "sensitive_data",
        "tool_access",
        "write_permission",
        "model_revision",
        "model_artifact_digest",
        "agent",
        "region",
    }
    assert all(key in known_fields for key in payload)


@given(st.lists(st.text(max_size=30), min_size=0, max_size=20))
@settings(max_examples=150)
def test_binding_payload_normalizes_groups(groups: list[str]) -> None:
    """Groups field is normalized to a sorted list."""
    request = {"groups": groups}
    payload = binding_payload(request)

    result_groups = payload.get("groups")
    assert isinstance(result_groups, list)
    assert result_groups == sorted(str(item) for item in groups)


def test_binding_payload_groups_none_and_scalar_contract() -> None:
    """None means no groups; a non-iterable scalar is rejected, never silently coerced.

    Coercing a tampered scalar to an empty list would make two different requests
    share one digest, so the payload builder keeps raising TypeError here.
    """
    assert binding_payload({"groups": None})["groups"] == []
    assert binding_payload({})["groups"] == []
    for bad in (123, True):
        with pytest.raises(TypeError):
            binding_payload({"groups": bad})


@given(st.one_of(st.text(), st.integers(), st.none()))
@settings(max_examples=150)
def test_binding_payload_tenant_id_fallback(tenant_value) -> None:
    """tenant_id normalizes from team field if not present."""
    request = {"team": tenant_value}
    payload = binding_payload(request)

    expected = str(tenant_value or "")
    assert payload.get("tenant_id") == expected


@given(st.booleans())
@settings(max_examples=50)
def test_binding_payload_preserves_booleans(bool_value: bool) -> None:
    """Boolean values are preserved as-is."""
    request = {"write_permission": bool_value}
    payload = binding_payload(request)
    assert payload.get("write_permission") is bool_value


@given(
    st.dictionaries(
        st.text(min_size=1, max_size=20),
        st.one_of(st.text(max_size=50), st.integers(), st.booleans(), st.none()),
        min_size=0,
        max_size=30,
    )
)
@settings(max_examples=200)
def test_compute_request_digest_produces_valid_hex(request: dict) -> None:
    """Digest is always valid hex string of correct length."""
    digest = compute_request_digest(request)
    assert isinstance(digest, str)
    assert len(digest) == 64
    assert all(c in "0123456789abcdef" for c in digest)


@given(
    st.dictionaries(
        st.text(min_size=1, max_size=20),
        st.text(max_size=50),
        min_size=1,
        max_size=20,
    )
)
@settings(max_examples=150)
def test_compute_request_digest_manual_verification(request: dict) -> None:
    """Digest can be manually verified against SHA-256."""
    digest = compute_request_digest(request)
    payload = binding_payload(request)
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    expected = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    assert digest == expected


@given(
    st.dictionaries(
        st.text(min_size=1, max_size=20),
        st.integers(min_value=-1000000, max_value=1000000),
        min_size=1,
        max_size=20,
    )
)
@settings(max_examples=150)
def test_binding_payload_handles_numeric_fields(request: dict) -> None:
    """Numeric fields are preserved in binding payload."""
    payload = binding_payload(request)

    # Check that numeric fields in known binding fields are preserved
    numeric_binding_fields = {
        "input_tokens",
        "output_tokens",
        "cost_per_request_usd",
        "cost_per_hour_usd",
        "month_to_date_cost_usd",
        "forecast_monthly_cost_usd",
    }

    for key in numeric_binding_fields:
        if key in request:
            assert key in payload


@given(
    st.text(max_size=1000),
    st.text(max_size=1000),
)
@settings(max_examples=200)
def test_compute_request_digest_different_subjects_different_digest(
    subject1: str, subject2: str
) -> None:
    """Different subjects produce different digests (unless identical)."""
    if subject1 == subject2:
        return

    request1 = {"subject": subject1}
    request2 = {"subject": subject2}

    digest1 = compute_request_digest(request1)
    digest2 = compute_request_digest(request2)

    assert digest1 != digest2
