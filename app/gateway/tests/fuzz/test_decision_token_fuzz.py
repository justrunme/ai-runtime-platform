"""Fuzz tests for decision token validation and digest normalization."""

from __future__ import annotations

import pytest
from hypothesis import assume, given, settings
from hypothesis import strategies as st

from app.gateway.decision_token import _normalize_digest


@given(st.text())
@settings(max_examples=200)
def test_normalize_digest_never_crashes(value: str) -> None:
    """Arbitrary strings never crash digest normalization."""
    try:
        result = _normalize_digest(value)
        assert isinstance(result, str)
    except Exception as error:
        pytest.fail(f"Unexpected error: {error}")


@given(st.text(alphabet="0123456789abcdef", min_size=64, max_size=64))
@settings(max_examples=200)
def test_normalize_digest_with_sha256_prefix(digest_hex: str) -> None:
    """sha256: prefix is correctly removed."""
    value = f"sha256:{digest_hex}"
    result = _normalize_digest(value)
    assert result == digest_hex


@given(st.text(alphabet="0123456789abcdef", min_size=64, max_size=64))
@settings(max_examples=200)
def test_normalize_digest_without_prefix(digest_hex: str) -> None:
    """Digest without prefix is returned as-is after strip."""
    result = _normalize_digest(digest_hex)
    assert result == digest_hex


@given(st.text())
@settings(max_examples=200)
def test_normalize_digest_strips_whitespace(value: str) -> None:
    """Leading/trailing whitespace is stripped."""
    result = _normalize_digest(value)
    assert result == value.strip().removeprefix("sha256:")


@given(st.text(min_size=1, max_size=200))
@settings(max_examples=300)
def test_normalize_digest_is_deterministic(value: str) -> None:
    """Same input always produces same output."""
    result1 = _normalize_digest(value)
    result2 = _normalize_digest(value)
    assert result1 == result2


@given(
    st.text(alphabet="0123456789abcdef", min_size=64, max_size=64),
    st.text(min_size=0, max_size=10, alphabet=" \t\n\r"),
)
@settings(max_examples=200)
def test_normalize_digest_with_surrounding_whitespace(digest: str, whitespace: str) -> None:
    """Whitespace around digest and prefix is handled correctly."""
    value = f"{whitespace}sha256:{digest}{whitespace}"
    result = _normalize_digest(value)
    assert result == digest


def test_normalize_digest_empty_string() -> None:
    """Empty string returns empty string."""
    result = _normalize_digest("")
    assert result == ""


def test_normalize_digest_only_whitespace() -> None:
    """Only whitespace returns empty string."""
    result = _normalize_digest("   \t\n   ")
    assert result == ""


def test_normalize_digest_only_prefix() -> None:
    """Only 'sha256:' returns empty string."""
    result = _normalize_digest("sha256:")
    assert result == ""


@given(st.text(min_size=1, max_size=100))
@settings(max_examples=200)
def test_normalize_digest_multiple_prefixes(digest: str) -> None:
    """Multiple sha256: prefixes are handled (only first is removed)."""
    value = f"sha256:sha256:{digest}"
    result = _normalize_digest(value)
    # After stripping and removing first prefix, we get "sha256:<digest>" but stripped
    expected = f"sha256:{digest}".strip()
    assert result == expected


@given(
    st.text(alphabet="0123456789ABCDEF", min_size=64, max_size=64),
)
@settings(max_examples=150)
def test_normalize_digest_uppercase_hex(digest_hex: str) -> None:
    """Uppercase hex digests are preserved (no case normalization)."""
    value = f"sha256:{digest_hex}"
    result = _normalize_digest(value)
    assert result == digest_hex


@given(
    st.text(alphabet="0123456789abcdef", min_size=1, max_size=200),
)
@settings(max_examples=200)
def test_normalize_digest_various_lengths(digest: str) -> None:
    """Digests of various lengths are handled (no length validation)."""
    value = f"sha256:{digest}"
    result = _normalize_digest(value)
    assert result == digest


@given(st.text(min_size=1, max_size=100))
@settings(max_examples=200)
def test_normalize_digest_non_hex_characters(value: str) -> None:
    """Non-hex characters are preserved (no character validation)."""
    assume("sha256:" not in value)
    result = _normalize_digest(value)
    assert result == value.strip()


@given(
    st.text(alphabet="0123456789abcdef", min_size=64, max_size=64),
    st.text(alphabet="0123456789abcdef", min_size=64, max_size=64),
)
@settings(max_examples=200)
def test_normalize_digest_different_values_different_result(digest1: str, digest2: str) -> None:
    """Different digests produce different normalized results."""
    assume(digest1 != digest2)

    result1 = _normalize_digest(f"sha256:{digest1}")
    result2 = _normalize_digest(f"sha256:{digest2}")

    assert result1 != result2


@given(st.text(alphabet="0123456789abcdef", min_size=64, max_size=64))
@settings(max_examples=200)
def test_normalize_digest_prefix_case_sensitive(digest: str) -> None:
    """Prefix is case-sensitive (SHA256: not removed)."""
    value = f"SHA256:{digest}"
    result = _normalize_digest(value)
    assert result == value.strip()


@given(
    st.text(min_size=1, max_size=50),
    st.text(min_size=1, max_size=50),
)
@settings(max_examples=200)
def test_normalize_digest_prefix_must_be_exact(prefix: str, digest: str) -> None:
    """Only exact 'sha256:' prefix is recognized."""
    assume(prefix != "sha256:")
    value = f"{prefix}{digest}"
    result = _normalize_digest(value)
    assert result == value.strip()


@given(st.text(max_size=200))
@settings(max_examples=200)
def test_normalize_digest_idempotent_after_first_call(value: str) -> None:
    """Normalizing twice produces same result as once."""
    result1 = _normalize_digest(value)
    result2 = _normalize_digest(result1)

    # If original had prefix, first call removes it, second doesn't change
    if value.strip().startswith("sha256:"):
        assert result1 == result2
    else:
        # Without prefix, both should be the same stripped value
        assert result1 == result2


@given(st.binary())
@settings(max_examples=150)
def test_normalize_digest_binary_data_decoded(data: bytes) -> None:
    """Binary data decoded as string is normalized."""
    value = data.decode("utf-8", errors="replace")
    try:
        result = _normalize_digest(value)
        assert isinstance(result, str)
    except Exception as error:
        pytest.fail(f"Unexpected error: {error}")
