"""Shared fixtures for gateway tests."""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _demo_trusted_proxy(monkeypatch: pytest.MonkeyPatch) -> None:
    """Default local/demo identity mode for existing suite; strict JWT tests override."""
    monkeypatch.setenv("IDENTITY_TRUSTED_PROXY", "true")
    monkeypatch.delenv("OIDC_JWT_VERIFY", raising=False)
    monkeypatch.delenv("OIDC_JWKS_URL", raising=False)
