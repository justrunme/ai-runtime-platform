"""Signed runtime verification tokens for Control Plane closed-loop."""

from __future__ import annotations

import json

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from httpx import ASGITransport, AsyncClient
from jwt.algorithms import RSAAlgorithm

from app.gateway.config import GatewaySettings, ModelTarget
from app.gateway.decisions import InMemoryDecisionStore
from app.gateway.main import app
from app.gateway.runtime_config import RuntimeConfigState
from app.gateway.stores.health import BackendHealthStore
from app.gateway.verify_signing import reset_verify_signing_cache


@pytest.fixture
def rsa_pem(monkeypatch: pytest.MonkeyPatch) -> str:
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode()
    monkeypatch.setenv("RUNTIME_VERIFY_PRIVATE_KEY", pem)
    monkeypatch.setenv("RUNTIME_VERIFY_KID", "test-verify")
    monkeypatch.setenv("RUNTIME_VERIFY_ISSUER", "ai-runtime")
    monkeypatch.setenv("RUNTIME_VERIFY_AUDIENCE", "ai-control-plane")
    reset_verify_signing_cache()
    yield pem
    reset_verify_signing_cache()


@pytest.fixture
async def runtime_client(rsa_pem: str):
    import httpx

    settings = GatewaySettings(
        model_targets={
            "qwen": ModelTarget(
                url="http://primary/v1",
                input_cost_per_million=0,
                output_cost_per_million=0,
            )
        }
    )
    client = httpx.AsyncClient()
    app.state.settings = settings
    app.state.runtime_config = RuntimeConfigState.from_settings(settings)
    app.state.client = client
    app.state.backend_health = BackendHealthStore(settings, client)
    app.state.decision_store = InMemoryDecisionStore()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as api:
        yield api
    await client.aclose()


@pytest.mark.anyio
async def test_runtime_verify_includes_signed_token(runtime_client: AsyncClient) -> None:
    status = (await runtime_client.get("/v1/runtime/status")).json()
    digest = status["configuration"]["observed_digest"]
    response = await runtime_client.post(
        "/v1/runtime/verify",
        json={
            "expected": {"config_digest": digest},
            "remediation_id": "rem-1",
            "correlation_id": "corr-1",
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["verified"] is True
    assert body["correlation"]["config_digest"] == digest
    assert body["remediation_id"] == "rem-1"
    assert body["verification_token"]

    jwks = (await runtime_client.get("/v1/runtime/jwks")).json()
    key = RSAAlgorithm.from_jwk(json.dumps(jwks["keys"][0]))
    claims = jwt.decode(
        body["verification_token"],
        key=key,
        algorithms=["RS256"],
        audience="ai-control-plane",
        issuer="ai-runtime",
    )
    assert claims["typ"] == "runtime_verify"
    assert claims["verified"] is True
    assert claims["remediation_id"] == "rem-1"


@pytest.mark.anyio
async def test_runtime_jwks_is_public(runtime_client: AsyncClient) -> None:
    response = await runtime_client.get("/v1/runtime/jwks")
    assert response.status_code == 200
    assert "keys" in response.json()
