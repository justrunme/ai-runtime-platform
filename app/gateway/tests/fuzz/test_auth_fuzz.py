"""Fuzz tests for authentication and bearer token extraction."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from hypothesis import assume, given, settings
from hypothesis import strategies as st

from app.gateway.auth import _bearer_token, request_is_authorized


@given(st.text())
@settings(max_examples=200)
def test_bearer_token_extraction_never_crashes(auth_header: str) -> None:
    """Arbitrary authorization headers never crash token extraction."""
    request = MagicMock()
    request.headers.get.return_value = auth_header

    try:
        result = _bearer_token(request)
        assert isinstance(result, str)
    except Exception as error:
        pytest.fail(f"Unexpected error: {error}")


@given(st.text(min_size=1, max_size=200))
@settings(max_examples=300)
def test_bearer_token_extraction_with_bearer_prefix(token: str) -> None:
    """Bearer prefix is correctly stripped."""
    request = MagicMock()
    request.headers.get.return_value = f"Bearer {token}"

    result = _bearer_token(request)
    assert result == token.strip()


@given(st.text(min_size=1, max_size=200))
@settings(max_examples=200)
def test_bearer_token_extraction_case_insensitive(token: str) -> None:
    """Bearer prefix is case-insensitive."""
    request = MagicMock()
    request.headers.get.return_value = f"bearer {token}"

    result = _bearer_token(request)
    assert result == token.strip()


@given(st.text())
@settings(max_examples=200)
def test_bearer_token_extraction_without_bearer_returns_empty(header: str) -> None:
    """Non-Bearer headers return empty string."""
    assume(not header.lower().startswith("bearer "))

    request = MagicMock()
    request.headers.get.return_value = header

    result = _bearer_token(request)
    assert result == ""


@given(st.text(min_size=1, max_size=200))
@settings(max_examples=200)
def test_bearer_token_extraction_whitespace_handling(token: str) -> None:
    """Leading/trailing whitespace in token is stripped."""
    request = MagicMock()
    request.headers.get.return_value = f"Bearer   {token}   "

    result = _bearer_token(request)
    assert result == token.strip()


def test_bearer_token_extraction_empty_header() -> None:
    """Empty authorization header returns empty string."""
    request = MagicMock()
    request.headers.get.return_value = ""

    result = _bearer_token(request)
    assert result == ""


def test_bearer_token_extraction_bearer_only() -> None:
    """Just 'Bearer' without token returns empty string."""
    request = MagicMock()
    request.headers.get.return_value = "Bearer"

    result = _bearer_token(request)
    assert result == ""


@given(
    st.text(
        alphabet="abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_",
        min_size=1,
        max_size=100,
    )
)
@settings(max_examples=200)
def test_request_is_authorized_bearer_token(api_key: str) -> None:
    """Bearer token authorization check."""
    request = MagicMock()
    request.headers.get.side_effect = lambda key, default="": {
        "authorization": f"Bearer {api_key}",
        "x-api-key": "",
    }.get(key, default)

    api_keys = frozenset([api_key])
    result = request_is_authorized(request, api_keys)
    assert result is True


@given(
    st.text(
        alphabet="abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_",
        min_size=1,
        max_size=100,
    )
)
@settings(max_examples=200)
def test_request_is_authorized_x_api_key(api_key: str) -> None:
    """X-API-Key header authorization check."""
    request = MagicMock()
    request.headers.get.side_effect = lambda key, default="": {
        "authorization": "",
        "x-api-key": api_key,
    }.get(key, default)

    api_keys = frozenset([api_key])
    result = request_is_authorized(request, api_keys)
    assert result is True


@given(
    st.text(min_size=1, max_size=100),
    st.text(min_size=1, max_size=100),
)
@settings(max_examples=200)
def test_request_is_authorized_wrong_key_rejected(provided_key: str, valid_key: str) -> None:
    """Wrong API key is rejected."""
    assume(provided_key != valid_key)

    request = MagicMock()
    request.headers.get.side_effect = lambda key, default="": {
        "authorization": f"Bearer {provided_key}",
        "x-api-key": "",
    }.get(key, default)

    api_keys = frozenset([valid_key])
    result = request_is_authorized(request, api_keys)
    assert result is False


@given(
    st.lists(
        st.text(
            alphabet="abcdefghijklmnopqrstuvwxyz0123456789",
            min_size=10,
            max_size=50,
        ),
        min_size=1,
        max_size=20,
        unique=True,
    ),
    st.integers(min_value=0, max_value=19),
)
@settings(max_examples=200)
def test_request_is_authorized_multiple_keys(api_keys_list: list[str], key_index: int) -> None:
    """Any key from the set authorizes the request."""
    if key_index >= len(api_keys_list):
        key_index = 0

    selected_key = api_keys_list[key_index]
    request = MagicMock()
    request.headers.get.side_effect = lambda key, default="": {
        "authorization": f"Bearer {selected_key}",
        "x-api-key": "",
    }.get(key, default)

    api_keys = frozenset(api_keys_list)
    result = request_is_authorized(request, api_keys)
    assert result is True


def test_request_is_authorized_empty_keys() -> None:
    """Empty API key set rejects all requests."""
    request = MagicMock()
    request.headers.get.side_effect = lambda key, default="": {
        "authorization": "Bearer sometoken",
        "x-api-key": "somekey",
    }.get(key, default)

    api_keys = frozenset([])
    result = request_is_authorized(request, api_keys)
    assert result is False


@given(st.text())
@settings(max_examples=200)
def test_bearer_token_extraction_deterministic(auth_header: str) -> None:
    """Token extraction is deterministic for same header."""
    request = MagicMock()
    request.headers.get.return_value = auth_header

    result1 = _bearer_token(request)
    result2 = _bearer_token(request)

    assert result1 == result2


@given(
    st.text(min_size=1, max_size=100),
)
@settings(max_examples=150)
def test_bearer_token_no_basic_auth_confusion(credentials: str) -> None:
    """Basic auth is not confused with Bearer."""
    request = MagicMock()
    request.headers.get.return_value = f"Basic {credentials}"

    result = _bearer_token(request)
    assert result == ""


@given(
    st.text(min_size=1, max_size=100),
)
@settings(max_examples=150)
def test_bearer_token_multiple_bearer_words(token: str) -> None:
    """Multiple 'Bearer' words are handled correctly."""
    request = MagicMock()
    request.headers.get.return_value = f"Bearer Bearer {token}"

    result = _bearer_token(request)
    # Should extract everything after first "Bearer "
    assert "Bearer" in result or result == token.strip()


@given(st.text(min_size=1, max_size=100))
@settings(max_examples=150)
def test_request_is_authorized_whitespace_in_key(api_key: str) -> None:
    """Whitespace in bearer tokens is stripped, but API keys are exact."""
    request = MagicMock()
    request.headers.get.side_effect = lambda key, default="": {
        "authorization": f"Bearer {api_key}",
        "x-api-key": "",
    }.get(key, default)

    # Bearer token is stripped
    api_keys = frozenset([api_key.strip()])
    result = request_is_authorized(request, api_keys)
    assert result is True
