"""GATEWAY_PROFILE security guards."""

from __future__ import annotations

import pytest

from app.gateway.main import GatewaySettings


def test_local_profile_allows_open_gateway(monkeypatch) -> None:
    monkeypatch.setenv("GATEWAY_PROFILE", "local")
    monkeypatch.delenv("GATEWAY_API_KEYS", raising=False)
    monkeypatch.delenv("OIDC_JWT_VERIFY", raising=False)
    monkeypatch.delenv("CONTROL_PLANE_URL", raising=False)
    monkeypatch.delenv("REDIS_URL", raising=False)
    monkeypatch.setenv("GATEWAY_REPLICAS", "1")
    monkeypatch.delenv("REQUIRE_SHARED_STATE", raising=False)
    settings = GatewaySettings.from_environment()
    assert settings.profile == "local"
    assert settings.require_auth is False


def test_internal_profile_requires_auth(monkeypatch) -> None:
    monkeypatch.setenv("GATEWAY_PROFILE", "internal")
    monkeypatch.delenv("GATEWAY_API_KEYS", raising=False)
    monkeypatch.delenv("OIDC_JWT_VERIFY", raising=False)
    with pytest.raises(ValueError, match="authentication is required"):
        GatewaySettings.from_environment()


def test_internal_profile_accepts_api_keys(monkeypatch) -> None:
    monkeypatch.setenv("GATEWAY_PROFILE", "internal")
    monkeypatch.setenv("GATEWAY_API_KEYS", "secret-key")
    monkeypatch.delenv("CONTROL_PLANE_URL", raising=False)
    settings = GatewaySettings.from_environment()
    assert settings.require_auth is True
    assert "secret-key" in settings.api_keys


def test_production_profile_fail_closed_without_jwt(monkeypatch) -> None:
    monkeypatch.setenv("GATEWAY_PROFILE", "production")
    monkeypatch.setenv("GATEWAY_API_KEYS", "secret-key")
    monkeypatch.setenv("CONTROL_PLANE_URL", "http://cp")
    monkeypatch.setenv("REDIS_URL", "redis://redis:6379/0")
    monkeypatch.delenv("OIDC_JWT_VERIFY", raising=False)
    with pytest.raises(ValueError, match="OIDC_JWT_VERIFY"):
        GatewaySettings.from_environment()


def test_production_profile_requires_redis_and_control_plane(monkeypatch) -> None:
    monkeypatch.setenv("GATEWAY_PROFILE", "production")
    monkeypatch.setenv("OIDC_JWT_VERIFY", "true")
    monkeypatch.setenv("OIDC_JWKS_URL", "https://issuer/.well-known/jwks.json")
    monkeypatch.delenv("CONTROL_PLANE_URL", raising=False)
    monkeypatch.setenv("REDIS_URL", "redis://redis:6379/0")
    with pytest.raises(ValueError, match="CONTROL_PLANE_URL"):
        GatewaySettings.from_environment()


def test_production_profile_accepts_full_config(monkeypatch) -> None:
    monkeypatch.setenv("GATEWAY_PROFILE", "production")
    monkeypatch.setenv("OIDC_JWT_VERIFY", "true")
    monkeypatch.setenv("OIDC_JWKS_URL", "https://issuer/.well-known/jwks.json")
    monkeypatch.setenv("CONTROL_PLANE_URL", "http://cp")
    monkeypatch.setenv("REDIS_URL", "rediss://:pass@redis:6379/0")
    settings = GatewaySettings.from_environment()
    assert settings.profile == "production"
    assert settings.require_shared_state is True
    assert settings.jwt_verify_enabled is True
    assert settings.control_plane_configured is True
