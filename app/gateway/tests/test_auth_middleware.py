"""Authentication boundary: JWT/API key protect all non-public routes."""

from __future__ import annotations

import time
from unittest.mock import AsyncMock

import httpx
import pytest

from app.gateway.jwt_verify import clear_jwks_client_cache
from app.gateway.main import (
    BackendHealthStore,
    GatewaySettings,
    ModelTarget,
    app,
    create_decision_store,
)
from app.gateway.tenant import create_tenant_store


def _bootstrap() -> httpx.AsyncClient:
    settings = GatewaySettings(
        model_targets={
            "qwen": ModelTarget(
                url="http://primary/v1",
                input_cost_per_million=0,
                output_cost_per_million=0,
            )
        }
    )
    upstream = httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json={"usage": {}}, request=request)
        )
    )
    app.state.settings = settings
    app.state.client = upstream
    app.state.backend_health = BackendHealthStore(settings, upstream)
    app.state.decision_store = create_decision_store(None)
    app.state.governance = None
    app.state.tenant_attribution = create_tenant_store(None)
    return upstream


@pytest.fixture(autouse=True)
def _clear_jwks_cache() -> None:
    clear_jwks_client_cache()
    yield
    clear_jwks_client_cache()


def _jwt_settings(**overrides) -> GatewaySettings:
    values = {
        "model_targets": {
            "qwen": ModelTarget(
                url="http://primary/v1",
                input_cost_per_million=0,
                output_cost_per_million=0,
            )
        },
        "jwt_verify_enabled": True,
        "jwt_issuer": "https://issuer.example",
        "jwt_audience": "ai-runtime",
        "require_jwt_iss_aud": True,
        "require_auth": True,
        "profile": "production",
        "redis_url": "redis://redis:6379/0",
        "require_shared_state": True,
        "control_plane_configured": True,
        "require_control_plane": True,
    }
    values.update(overrides)
    return GatewaySettings(**values)


@pytest.mark.anyio
async def test_jwt_mode_blocks_models_without_token(monkeypatch) -> None:
    monkeypatch.setenv("OIDC_JWT_VERIFY", "true")
    monkeypatch.setenv("OIDC_JWKS_URL", "https://issuer.example/jwks")
    monkeypatch.delenv("OIDC_JWT_REQUIRE_ISS_AUD", raising=False)
    upstream = _bootstrap()
    app.state.settings = _jwt_settings()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://gw") as client:
        for path in (
            "/v1/models",
            "/v1/routes",
            "/v1/backends/health",
            "/v1/decisions/abc",
        ):
            response = await client.get(path)
            assert response.status_code == 401, path
            body = response.json()
            assert "detail" not in body
            assert body["error"]["code"] == "missing_bearer_token"
        livez = await client.get("/livez")
        assert livez.status_code == 200
    await upstream.aclose()


@pytest.mark.anyio
async def test_jwt_mode_accepts_valid_token_on_models(monkeypatch) -> None:
    monkeypatch.setenv("OIDC_JWT_VERIFY", "true")
    monkeypatch.setenv("OIDC_JWKS_URL", "https://issuer.example/jwks")

    async def fake_verify(token: str) -> dict:
        assert token == "good-token"
        return {
            "sub": "svc-1",
            "iss": "https://issuer.example",
            "aud": "ai-runtime",
            "exp": int(time.time()) + 60,
            "iat": int(time.time()),
        }

    monkeypatch.setattr(
        "app.gateway.auth.verify_bearer_token_async",
        AsyncMock(side_effect=fake_verify),
    )
    upstream = _bootstrap()
    app.state.settings = _jwt_settings()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://gw") as client:
        response = await client.get("/v1/models", headers={"Authorization": "Bearer good-token"})
        assert response.status_code == 200
        assert "qwen" in {item["id"] for item in response.json()["data"]}
    await upstream.aclose()


@pytest.mark.anyio
async def test_jwt_mode_rejects_invalid_token(monkeypatch) -> None:
    monkeypatch.setenv("OIDC_JWT_VERIFY", "true")
    monkeypatch.setenv("OIDC_JWKS_URL", "https://issuer.example/jwks")

    import jwt as pyjwt

    monkeypatch.setattr(
        "app.gateway.auth.verify_bearer_token_async",
        AsyncMock(side_effect=pyjwt.InvalidTokenError("bad")),
    )
    upstream = _bootstrap()
    app.state.settings = _jwt_settings()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://gw") as client:
        response = await client.get("/v1/models", headers={"Authorization": "Bearer bad-token"})
        assert response.status_code == 401
        assert response.json()["error"]["code"] == "invalid_bearer_token"
    await upstream.aclose()


@pytest.mark.anyio
async def test_api_key_mode_still_protects_routes(monkeypatch) -> None:
    monkeypatch.delenv("OIDC_JWT_VERIFY", raising=False)
    monkeypatch.delenv("OIDC_JWT_REQUIRE_ISS_AUD", raising=False)
    upstream = _bootstrap()
    app.state.settings = GatewaySettings(
        model_targets={
            "qwen": ModelTarget(
                url="http://primary/v1",
                input_cost_per_million=0,
                output_cost_per_million=0,
            )
        },
        api_keys=frozenset({"secret-key"}),
        require_auth=True,
        profile="internal",
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://gw") as client:
        denied = await client.get("/v1/models")
        assert denied.status_code == 401
        allowed = await client.get("/v1/models", headers={"Authorization": "Bearer secret-key"})
        assert allowed.status_code == 200
    await upstream.aclose()
