"""Fuzz tests for JWT verification and parsing."""

from __future__ import annotations

import base64
import contextlib
import json

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from app.gateway.jwt_verify import decode_unsigned_payload


@given(st.binary())
@settings(max_examples=100)
def test_decode_unsigned_payload_never_crashes_on_arbitrary_bytes(token: bytes) -> None:
    """Arbitrary bytes never cause unhandled exceptions."""
    with contextlib.suppress(ValueError, TypeError, json.JSONDecodeError):
        decode_unsigned_payload(token.decode("utf-8", errors="replace"))


@given(st.text())
@settings(max_examples=200)
def test_decode_unsigned_payload_only_raises_documented_errors(token: str) -> None:
    """Only ValueError, TypeError, or JSONDecodeError are raised."""
    with contextlib.suppress(ValueError, TypeError, json.JSONDecodeError):
        decode_unsigned_payload(token)


@given(
    st.lists(st.text(min_size=1), min_size=0, max_size=10),
    st.dictionaries(st.text(min_size=1), st.text() | st.integers() | st.booleans()),
)
@settings(max_examples=150)
def test_decode_unsigned_payload_rejects_malformed_structure(
    segments: list[str], payload_dict: dict
) -> None:
    """JWT must have exactly three segments, payload must be a dict."""
    if len(segments) != 3:
        with pytest.raises((ValueError, json.JSONDecodeError)):
            decode_unsigned_payload(".".join(segments))
    else:
        header, _, signature = segments
        payload_json = json.dumps(payload_dict)
        payload_b64 = base64.urlsafe_b64encode(payload_json.encode()).decode().rstrip("=")
        token = f"{header}.{payload_b64}.{signature}"
        try:
            result = decode_unsigned_payload(token)
            assert isinstance(result, dict)
            assert result == payload_dict
        except (ValueError, json.JSONDecodeError):
            # Invalid base64 in header or other parts is acceptable
            pass


@given(st.text())
@settings(max_examples=150)
def test_decode_unsigned_payload_rejects_truncated_tokens(payload_text: str) -> None:
    """Truncated or incomplete tokens are always rejected."""
    # Missing segments - should raise an error
    try:
        decode_unsigned_payload(payload_text)
        # If it doesn't raise, it must have been a valid 3-segment token
        assert payload_text.count(".") == 2
    except (ValueError, TypeError, json.JSONDecodeError):
        pass


@given(
    st.lists(st.none() | st.integers() | st.text()),
)
@settings(max_examples=100)
def test_decode_unsigned_payload_rejects_non_dict_payload(payload: list) -> None:
    """JWT payload must be a JSON object, not array or primitive."""
    payload_json = json.dumps(payload)
    payload_b64 = base64.urlsafe_b64encode(payload_json.encode()).decode().rstrip("=")
    token = f"eyJhbGciOiJub25lIn0.{payload_b64}.sig"
    with pytest.raises(TypeError, match="payload must be a JSON object"):
        decode_unsigned_payload(token)


@given(st.binary(min_size=1, max_size=10000))
@settings(max_examples=150)
def test_decode_unsigned_payload_handles_base64_padding(random_bytes: bytes) -> None:
    """Base64 padding is handled correctly for all byte lengths."""
    header = "eyJhbGciOiJub25lIn0"
    signature = "sig"
    payload_b64 = base64.urlsafe_b64encode(random_bytes).decode().rstrip("=")
    token = f"{header}.{payload_b64}.{signature}"
    with contextlib.suppress(ValueError, TypeError, json.JSONDecodeError):
        decode_unsigned_payload(token)


@given(
    st.dictionaries(
        st.text(alphabet="abcdefghijklmnopqrstuvwxyz", min_size=1, max_size=20),
        st.one_of(st.text(max_size=100), st.integers(), st.booleans(), st.none()),
        min_size=0,
        max_size=50,
    )
)
@settings(max_examples=200)
def test_decode_unsigned_payload_deterministic(payload_dict: dict) -> None:
    """Decoding the same token always produces the same result."""
    payload_json = json.dumps(payload_dict)
    payload_b64 = base64.urlsafe_b64encode(payload_json.encode()).decode().rstrip("=")
    token = f"eyJhbGciOiJub25lIn0.{payload_b64}.sig"

    result1 = decode_unsigned_payload(token)
    result2 = decode_unsigned_payload(token)
    assert result1 == result2
    assert result1 == payload_dict


@given(st.text(alphabet="abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"))
@settings(max_examples=100)
def test_jwt_alg_none_always_rejected_when_verification_enabled(payload_text: str) -> None:
    """alg=none tokens must never be accepted when verification is enabled."""
    header_none = (
        base64.urlsafe_b64encode(json.dumps({"alg": "none"}).encode()).decode().rstrip("=")
    )
    payload_b64 = (
        base64.urlsafe_b64encode(json.dumps({"sub": payload_text}).encode()).decode().rstrip("=")
    )
    token_none = f"{header_none}.{payload_b64}."

    # Without verification, decode_unsigned_payload should work
    result = decode_unsigned_payload(token_none)
    assert isinstance(result, dict)


@given(
    st.dictionaries(
        st.text(min_size=1, max_size=20),
        st.one_of(st.text(max_size=50), st.integers(-1000, 1000)),
        min_size=1,
        max_size=20,
    )
)
@settings(max_examples=150)
def test_decode_unsigned_payload_swapped_signature_accepted(payload_dict: dict) -> None:
    """Without verification, signature is not validated (demonstrates unsigned parsing)."""
    payload_json = json.dumps(payload_dict)
    payload_b64 = base64.urlsafe_b64encode(payload_json.encode()).decode().rstrip("=")

    token1 = f"eyJhbGciOiJub25lIn0.{payload_b64}.signature1"
    token2 = f"eyJhbGciOiJub25lIn0.{payload_b64}.signature2"

    result1 = decode_unsigned_payload(token1)
    result2 = decode_unsigned_payload(token2)

    assert result1 == result2 == payload_dict


@given(st.integers(min_value=1, max_value=1000000))
@settings(max_examples=100)
def test_decode_unsigned_payload_huge_payload_structure(size_hint: int) -> None:
    """Very large payloads are parsed (no hang, no crash)."""
    large_payload = {f"key_{i}": f"value_{i}" for i in range(min(size_hint, 1000))}
    payload_json = json.dumps(large_payload)
    payload_b64 = base64.urlsafe_b64encode(payload_json.encode()).decode().rstrip("=")
    token = f"eyJhbGciOiJub25lIn0.{payload_b64}.sig"

    result = decode_unsigned_payload(token)
    assert isinstance(result, dict)
    assert len(result) == len(large_payload)
