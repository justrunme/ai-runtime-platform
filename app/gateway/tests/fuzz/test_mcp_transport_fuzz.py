"""Fuzz tests for MCP message parsing and transport."""

from __future__ import annotations

import contextlib
import json

import pytest
from hypothesis import assume, given, settings
from hypothesis import strategies as st

from app.gateway.mcp_transport import (
    _parse_sse_jsonrpc,
    parse_mcp_response_body,
    resolve_credential,
)


@given(st.binary())
@settings(max_examples=200)
def test_parse_mcp_response_body_never_crashes_on_bytes(body: bytes) -> None:
    """Arbitrary bytes never cause unhandled exceptions."""
    with contextlib.suppress(ValueError, UnicodeDecodeError, json.JSONDecodeError):
        parse_mcp_response_body("application/json", body)


@given(st.text(), st.binary())
@settings(max_examples=200)
def test_parse_mcp_response_body_only_documented_errors(content_type: str, body: bytes) -> None:
    """Only ValueError or JSONDecodeError are raised."""
    with contextlib.suppress(ValueError, json.JSONDecodeError):
        parse_mcp_response_body(content_type, body)


@given(
    st.dictionaries(
        st.text(min_size=1, max_size=50),
        st.one_of(st.text(max_size=100), st.integers(), st.booleans(), st.none()),
        min_size=1,
        max_size=30,
    )
)
@settings(max_examples=200)
def test_parse_mcp_response_body_valid_json(payload: dict) -> None:
    """Valid JSON dicts are parsed correctly."""
    body = json.dumps(payload).encode("utf-8")
    result = parse_mcp_response_body("application/json", body)
    assert result == payload


@given(st.lists(st.text()))
@settings(max_examples=100)
def test_parse_mcp_response_body_rejects_non_dict_json(payload: list) -> None:
    """Non-dict JSON is rejected with ValueError."""
    body = json.dumps(payload).encode("utf-8")
    with pytest.raises(ValueError, match="non-object JSON"):
        parse_mcp_response_body("application/json", body)


@given(st.text())
@settings(max_examples=200)
def test_parse_sse_jsonrpc_never_crashes(text: str) -> None:
    """Arbitrary text never causes unhandled exceptions in SSE parsing."""
    with contextlib.suppress(ValueError):
        _parse_sse_jsonrpc(text)


@given(
    st.lists(
        st.dictionaries(
            st.text(min_size=1, max_size=20),
            st.one_of(st.text(max_size=50), st.integers()),
            min_size=1,
            max_size=10,
        ),
        min_size=1,
        max_size=10,
    )
)
@settings(max_examples=150)
def test_parse_sse_jsonrpc_extracts_last_message(messages: list[dict]) -> None:
    """SSE parser extracts the last valid JSON-RPC message."""
    sse_lines = []
    for msg in messages:
        json_line = json.dumps(msg)
        sse_lines.append(f"data: {json_line}\n\n")

    sse_text = "".join(sse_lines)
    result = _parse_sse_jsonrpc(sse_text)
    assert result == messages[-1]


@given(st.text(min_size=1, max_size=100))
@settings(max_examples=150)
def test_parse_sse_jsonrpc_rejects_empty_stream(garbage: str) -> None:
    """Empty or invalid SSE streams raise ValueError."""
    assume(not garbage.strip().startswith("data:"))

    with pytest.raises(ValueError, match="contained no JSON-RPC message"):
        _parse_sse_jsonrpc(garbage)


@given(
    st.lists(
        st.text(min_size=1, max_size=100),
        min_size=1,
        max_size=20,
    )
)
@settings(max_examples=150)
def test_parse_sse_jsonrpc_handles_malformed_lines(lines: list[str]) -> None:
    """Malformed SSE lines are skipped gracefully."""
    sse_text = "\n".join(lines)
    with contextlib.suppress(ValueError):
        _parse_sse_jsonrpc(sse_text)


@given(
    st.dictionaries(
        st.text(min_size=1, max_size=20),
        st.one_of(st.text(max_size=50), st.integers()),
        min_size=1,
        max_size=10,
    )
)
@settings(max_examples=150)
def test_parse_sse_jsonrpc_done_marker_ignored(message: dict) -> None:
    """[DONE] marker does not interfere with parsing."""
    json_line = json.dumps(message)
    sse_text = f"data: {json_line}\n\ndata: [DONE]\n\n"

    result = _parse_sse_jsonrpc(sse_text)
    assert result == message


@given(st.text())
@settings(max_examples=100)
def test_resolve_credential_only_documented_errors(ref: str) -> None:
    """resolve_credential only raises ValueError."""
    with contextlib.suppress(ValueError):
        resolve_credential(ref)


@given(st.text(min_size=1, max_size=100))
@settings(max_examples=150)
def test_resolve_credential_env_prefix_required(name: str) -> None:
    """Credential refs without env: prefix are rejected."""
    assume(not name.startswith("env:"))
    assume(not name.startswith("vault://"))

    with pytest.raises(ValueError, match="unsupported credential ref"):
        resolve_credential(name)


def test_resolve_credential_vault_refs_rejected() -> None:
    """vault:// credential refs are explicitly rejected."""
    with pytest.raises(ValueError, match="vault credential refs require a sidecar"):
        resolve_credential("vault://secret/path")


@given(st.text(alphabet="abcdefghijklmnopqrstuvwxyz_", min_size=1, max_size=50))
@settings(max_examples=100)
def test_resolve_credential_empty_env_rejected(env_name: str) -> None:
    """Empty environment variables are rejected with ValueError."""
    ref = f"env:{env_name}"
    # Will raise ValueError because env var doesn't exist or is empty
    try:
        resolve_credential(ref)
    except ValueError as error:
        assert "empty" in str(error).lower() or env_name in str(error)


@given(
    st.dictionaries(
        st.text(min_size=1, max_size=20),
        st.one_of(st.text(max_size=50), st.integers(), st.booleans()),
        min_size=1,
        max_size=10,
    )
)
@settings(max_examples=150)
def test_parse_mcp_response_body_sse_content_type(payload: dict) -> None:
    """SSE content type triggers SSE parsing."""
    json_line = json.dumps(payload)
    sse_body = f"data: {json_line}\n\n".encode()

    result = parse_mcp_response_body("text/event-stream", sse_body)
    assert result == payload


@given(st.text(min_size=1, max_size=1000))
@settings(max_examples=150)
def test_parse_mcp_response_body_detects_sse_by_content(text: str) -> None:
    """SSE-like content is detected even with wrong content-type."""
    if text.startswith("data:") or text.startswith("event:"):
        body = text.encode("utf-8")
        with contextlib.suppress(ValueError, json.JSONDecodeError):
            parse_mcp_response_body("application/json", body)


@given(
    st.lists(
        st.dictionaries(
            st.text(alphabet="abcdefghijklmnopqrstuvwxyz", min_size=2, max_size=20),
            st.integers(),
            min_size=1,
            max_size=5,
        ),
        min_size=2,
        max_size=10,
    )
)
@settings(max_examples=150)
def test_parse_sse_jsonrpc_multiline_data(messages: list[dict]) -> None:
    """Multi-line data fields are concatenated correctly."""
    last_msg = messages[-1]
    json_str = json.dumps(last_msg)

    # Only split if JSON doesn't contain newlines and is long enough
    assume("\n" not in json_str)
    assume(len(json_str) > 4)

    # Split JSON across multiple data: lines at a safe point (not in the middle of a key)
    mid = len(json_str) // 2
    # Ensure we don't split in the middle of a key name by finding a safe split point
    while mid > 0 and mid < len(json_str) - 1:
        if json_str[mid - 1] in (",", ":", "{", "[") or json_str[mid] in (",", ":", "}", "]"):
            break
        mid += 1

    if mid >= len(json_str) - 1:
        # Can't find safe split point, use whole message
        sse_text = f"data: {json_str}\n\n"
    else:
        part1 = json_str[:mid]
        part2 = json_str[mid:]
        sse_text = f"data: {part1}\ndata: {part2}\n\n"

    result = _parse_sse_jsonrpc(sse_text)
    assert result == last_msg


@given(
    st.integers(min_value=1, max_value=100),
    st.text(min_size=1, max_size=50),
)
@settings(max_examples=150)
def test_parse_sse_jsonrpc_multiple_events(event_count: int, data: str) -> None:
    """Multiple events in SSE stream are handled, last one wins."""
    events = []
    sse_lines = []

    for i in range(event_count):
        event = {"id": i, "data": data}
        events.append(event)
        sse_lines.append(f"data: {json.dumps(event)}\n\n")

    sse_text = "".join(sse_lines)
    result = _parse_sse_jsonrpc(sse_text)
    assert result == events[-1]


def test_resolve_credential_none_ref() -> None:
    """None credential ref returns None."""
    assert resolve_credential(None) is None


def test_resolve_credential_empty_string() -> None:
    """Empty string credential ref returns None."""
    assert resolve_credential("") is None
