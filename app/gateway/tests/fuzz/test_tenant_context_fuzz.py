"""Fuzz tests for tenant context extraction and isolation."""

from __future__ import annotations

from unittest.mock import MagicMock

from hypothesis import given, settings
from hypothesis import strategies as st

from app.gateway.tenant_context import _normalize_groups, resolve_tenant_id


@given(
    st.lists(st.text(max_size=100), min_size=0, max_size=50),
)
@settings(max_examples=200)
def test_normalize_groups_from_list(groups: list[str]) -> None:
    """Groups as list are normalized to stripped strings."""
    claims = {"groups": groups}
    result = _normalize_groups(claims)

    assert isinstance(result, list)
    expected = [str(item).strip() for item in groups if str(item).strip()]
    assert result == expected


@given(st.text(max_size=500))
@settings(max_examples=200)
def test_normalize_groups_from_comma_string(groups_str: str) -> None:
    """Groups as comma-separated string are split and normalized."""
    claims = {"groups": groups_str}
    result = _normalize_groups(claims)

    assert isinstance(result, list)
    expected = [part.strip() for part in groups_str.split(",") if part.strip()]
    assert result == expected


@given(st.text(max_size=500))
@settings(max_examples=200)
def test_normalize_groups_from_roles_fallback(roles_str: str) -> None:
    """Groups can be extracted from 'roles' field as fallback."""
    claims = {"roles": roles_str}
    result = _normalize_groups(claims)

    assert isinstance(result, list)


@given(
    st.one_of(
        st.integers(),
        st.booleans(),
        st.none(),
        st.dictionaries(st.text(), st.text()),
    )
)
@settings(max_examples=150)
def test_normalize_groups_invalid_types_return_empty(value) -> None:
    """Invalid group types return empty list without crashing."""
    claims = {"groups": value}
    result = _normalize_groups(claims)

    assert isinstance(result, list)
    if not isinstance(value, (str, list)):
        assert result == []


@given(st.text(max_size=200))
@settings(max_examples=200)
def test_resolve_tenant_id_from_claims(tenant_value: str) -> None:
    """Tenant ID is resolved from JWT claims when present."""
    request = MagicMock()
    request.state.identity_claims = {"tenant_id": tenant_value}

    result = resolve_tenant_id(request)

    if tenant_value.strip():
        assert result == tenant_value.strip()


@given(st.text(max_size=200))
@settings(max_examples=150)
def test_resolve_tenant_id_team_fallback(team_value: str) -> None:
    """Team field is used as fallback for tenant_id."""
    request = MagicMock()
    request.state.identity_claims = {"team": team_value}

    result = resolve_tenant_id(request)

    if team_value.strip():
        assert result == team_value.strip()


@given(st.text(max_size=200))
@settings(max_examples=150)
def test_resolve_tenant_id_tenant_fallback(tenant_value: str) -> None:
    """'tenant' field is checked as alternative."""
    request = MagicMock()
    request.state.identity_claims = {"tenant": tenant_value}

    result = resolve_tenant_id(request)

    if tenant_value.strip():
        assert result == tenant_value.strip()


@given(st.text(alphabet="abcdefghijklmnopqrstuvwxyz0123456789-_", min_size=1, max_size=100))
@settings(max_examples=200)
def test_resolve_tenant_id_never_empty_string(subject: str) -> None:
    """Tenant ID is never an empty string."""
    request = MagicMock()
    request.state.identity_claims = {"sub": subject}

    result = resolve_tenant_id(request)

    assert isinstance(result, str)
    assert len(result) > 0


@given(
    st.text(max_size=100),
    st.text(max_size=100),
)
@settings(max_examples=200)
def test_resolve_tenant_id_priority_order(tenant_id: str, team: str) -> None:
    """tenant_id has priority over team field, but whitespace-only values are skipped."""
    request = MagicMock()
    request.state.identity_claims = {
        "tenant_id": tenant_id,
        "team": team,
    }

    result = resolve_tenant_id(request)

    # After stripping, priority is tenant_id > team > platform
    if tenant_id.strip():
        assert result == tenant_id.strip()
    elif team.strip():
        assert result == team.strip()
    else:
        assert result == "platform"


def test_resolve_tenant_id_whitespace_only_regression() -> None:
    """Regression test: whitespace-only tenant_id should not return empty string.

    This bug was discovered by fuzz testing. Previously, a JWT with tenant_id='\r'
    would return an empty string, potentially breaking tenant isolation.
    """
    request = MagicMock()

    # Test various whitespace-only values
    for whitespace in ["\r", "\n", "\t", "  ", "\r\n\t "]:
        request.state.identity_claims = {"tenant_id": whitespace}
        result = resolve_tenant_id(request)
        assert result == "platform", (
            f"whitespace-only tenant_id '{repr(whitespace)}' should fall back to 'platform'"
        )

    # Test with team as fallback
    request.state.identity_claims = {"tenant_id": "\r", "team": "valid-team"}
    result = resolve_tenant_id(request)
    assert result == "valid-team"


def test_resolve_tenant_id_no_claims_returns_platform() -> None:
    """Without JWT verification and no claims, returns 'platform'."""
    request = MagicMock()
    request.state.identity_claims = {}

    result = resolve_tenant_id(request)
    assert result == "platform"


@given(
    st.lists(st.text(max_size=50), min_size=0, max_size=30),
)
@settings(max_examples=150)
def test_normalize_groups_deduplication(groups: list[str]) -> None:
    """Duplicate groups after stripping are not automatically deduplicated."""
    claims = {"groups": groups}
    result = _normalize_groups(claims)

    assert isinstance(result, list)
    # The function doesn't deduplicate, just normalizes
    assert len(result) == len([str(item).strip() for item in groups if str(item).strip()])


@given(
    st.text(max_size=100),
)
@settings(max_examples=150)
def test_normalize_groups_whitespace_handling(group_text: str) -> None:
    """Leading/trailing whitespace is stripped from groups."""
    claims = {"groups": [f"  {group_text}  "]}
    result = _normalize_groups(claims)

    if group_text.strip():
        assert result == [group_text.strip()]
    else:
        assert result == []


@given(
    st.text(alphabet="abcdefghijklmnopqrstuvwxyz", min_size=1, max_size=50),
    st.text(alphabet="abcdefghijklmnopqrstuvwxyz", min_size=1, max_size=50),
)
@settings(max_examples=200)
def test_resolve_tenant_id_isolation(tenant1: str, tenant2: str) -> None:
    """Different tenant IDs remain distinct (isolation property)."""
    if tenant1 == tenant2:
        return

    request1 = MagicMock()
    request1.state.identity_claims = {"tenant_id": tenant1}

    request2 = MagicMock()
    request2.state.identity_claims = {"tenant_id": tenant2}

    result1 = resolve_tenant_id(request1)
    result2 = resolve_tenant_id(request2)

    assert result1 != result2


@given(st.text(max_size=200))
@settings(max_examples=150)
def test_resolve_tenant_id_no_path_traversal(tenant_value: str) -> None:
    """Path traversal sequences in tenant IDs are preserved as-is (no normalization)."""
    request = MagicMock()
    request.state.identity_claims = {"tenant_id": tenant_value}

    result = resolve_tenant_id(request)

    # The function doesn't normalize path traversal - it's returned as-is
    if tenant_value.strip():
        assert result == tenant_value.strip()


@given(
    st.text(
        alphabet="abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_",
        min_size=1,
        max_size=100,
    )
)
@settings(max_examples=200)
def test_resolve_tenant_id_deterministic(tenant_id: str) -> None:
    """Resolving tenant ID is deterministic for same input."""
    request = MagicMock()
    request.state.identity_claims = {"tenant_id": tenant_id}

    result1 = resolve_tenant_id(request)
    result2 = resolve_tenant_id(request)

    assert result1 == result2
