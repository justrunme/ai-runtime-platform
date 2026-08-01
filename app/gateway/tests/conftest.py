"""Shared fixtures for gateway tests."""

from __future__ import annotations

import pytest

from app.gateway.main import app
from app.gateway.resilience import DrainState, GlobalAdmissionController


@pytest.fixture(autouse=True)
def _demo_trusted_proxy(monkeypatch: pytest.MonkeyPatch) -> None:
    """Default local/demo identity mode for existing suite; strict JWT tests override."""
    monkeypatch.setenv("IDENTITY_TRUSTED_PROXY", "true")
    monkeypatch.delenv("OIDC_JWT_VERIFY", raising=False)
    monkeypatch.delenv("OIDC_JWKS_URL", raising=False)


@pytest.fixture(autouse=True)
def _reset_resilience_state() -> None:
    """Prevent drain/load-shed state from leaking across ASGI app.state tests."""
    app.state.drain = DrainState()
    app.state.global_admission = GlobalAdmissionController(max_inflight=1024, max_queued=1024)
    yield
    app.state.drain = DrainState()
